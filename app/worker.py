"""
ARQ Worker — entry point for the ingestion worker process.

How ARQ works:
  - WorkerSettings tells ARQ which functions to expose as tasks
  - on_startup runs ONCE when the worker process starts
  - on_shutdown runs ONCE when the worker process stops
  - process_ingestion_task runs every time a job is dequeued from Redis

The worker context (ctx) is a dict that persists across all tasks in one
worker process. We store shared objects here — DB connection, services,
providers — so they are initialised ONCE, not per task.

Starting the worker:
  arq app.worker.WorkerSettings

The orchestrator enqueues jobs like this:
  await queue.enqueue_job("process_ingestion_task", job_id=..., repo_id=..., tenant_id=...)
The function name "process_ingestion_task" must match exactly.
"""

from datetime import datetime, timezone

import structlog
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.exceptions import RepoConfigNotFoundError, ConnectorError
from app.db.mongo import connect_to_mongo, close_mongo_connection, get_database
from app.db.indexes import create_indexes
from app.repositories.job_repo import JobRepository
from app.repositories.document_repo import DocumentRepository
from app.repositories.repo_config_repo import RepoConfigRepository
from app.connectors.factory import get_connector
from app.providers.embedding.openai_provider import OpenAIEmbeddingProvider
from app.providers.vectorstore.mongodb_atlas import MongoDBAtlasVectorStore
from app.services.ingestion_service import IngestionService
from app.services.job_lifecycle import JobLifecycleService

# Configure logging immediately — before any other imports log anything
setup_logging()
log = structlog.get_logger(__name__)


# ── Startup — runs once when the worker process starts ────────────────────────


async def startup(ctx: dict) -> None:
    """
    Initialise all shared resources and store them in ctx.
    ctx is passed to every task — objects here are created once, reused always.

    This is where the heavy work happens:
      - MongoDB connection established
      - MarkItDown / pymupdf4llm loaded (lazy, on first use)
      - OpenAI clients initialised
    """
    log.info("worker.starting", version=settings.VERSION, env=settings.ENVIRONMENT)

    # 1. MongoDB
    await connect_to_mongo()
    db = get_database()
    await create_indexes(db)

    # 2. Repositories — stateless, just hold a db reference
    ctx["job_repo"] = JobRepository(db)
    ctx["document_repo"] = DocumentRepository(db)
    ctx["repo_config_repo"] = RepoConfigRepository(db)

    # 3. Embedding provider — one OpenAI client for the whole worker process
    ctx["embedding_provider"] = OpenAIEmbeddingProvider(
        api_key=settings.OPENAI_API_KEY,
        model="text-embedding-3-small",
    )

    # 4. LLM for loader enrichment — used only when document complexity requires it:
    #    - Scanned PDFs (no text layer) → Vision OCR per page
    #    - Embedded images in PDF/DOCX  → Vision descriptions
    #    - Nested tables in DOCX        → LLM cleanup
    #    api_key is read automatically from OPENAI_API_KEY env var.
    #    If init fails, worker continues in library-only mode (graceful degradation).
    try:
        from langchain_openai import ChatOpenAI

        ctx["loader_llm"] = ChatOpenAI(
            model="gpt-4o",
            temperature=0,
            max_tokens=4096,
        )
        log.info("loader.llm.initialised", model="gpt-4o")
    except Exception as e:
        log.warning(
            "loader.llm.init_failed",
            error=str(e),
            message="LLM enrichment disabled — scanned PDFs and images won't be described",
        )
        ctx["loader_llm"] = None

    # 5. Job lifecycle service
    ctx["job_lifecycle"] = JobLifecycleService(ctx["job_repo"])

    log.info("worker.ready")


# ── Shutdown — runs once when the worker process stops ────────────────────────


async def shutdown(ctx: dict) -> None:
    log.info("worker.shutting_down")
    await close_mongo_connection()
    log.info("worker.stopped")


# ── Main task function ────────────────────────────────────────────────────────


async def process_ingestion_task(
    ctx: dict,
    job_id: str,
    repo_id: str,
    tenant_id: str,
) -> dict:
    """
    The main ingestion task. Called by ARQ for every job dequeued from Redis.

    Flow:
      1. Mark job as processing
      2. Load repo config + credentials from MongoDB
      3. Build per-task services (VectorStore, IngestionService)
      4. Connect to data source
      5. Stream documents — process upserts, handle explicit deletes
      6. Detect implicit deletes (connectors without native delete support)
      7. Save delta token if connector supports it
      8. Mark job completed with final stats

    Args:
      ctx       : ARQ worker context (shared across all tasks in this process)
      job_id    : UUID of the job record in MongoDB
      repo_id   : ID of the source_repository to ingest
      tenant_id : ID of the tenant that owns this repo

    Returns:
      dict with final job stats (also written to MongoDB)
    """
    bound_log = log.bind(job_id=job_id, repo_id=repo_id, tenant_id=tenant_id)
    bound_log.info("task.started")

    # Retrieve shared objects from worker context
    job_lifecycle: JobLifecycleService = ctx["job_lifecycle"]
    repo_config_repo: RepoConfigRepository = ctx["repo_config_repo"]
    document_repo: DocumentRepository = ctx["document_repo"]
    embedding_provider = ctx["embedding_provider"]

    # Mark job as processing — updates MongoDB status field
    stats = await job_lifecycle.start(job_id)

    try:
        # ── Step 1: Load repo config + credentials from MongoDB ───────────────
        # Credentials are always loaded fresh from DB — never put in the queue
        bound_log.info("task.loading_config")
        repo_config = await repo_config_repo.get_repo(repo_id)
        tenant_config = await repo_config_repo.get_tenant(tenant_id)
        credentials = await repo_config_repo.get_credentials(repo_id)

        # Resolve effective ingestion config (tenant defaults + repo overrides)
        tenant_defaults = tenant_config.get("ingestion_defaults", {})
        repo_overrides = repo_config.get("ingestion_overrides", {})
        chunk_size = repo_overrides.get("chunk_size") or tenant_defaults.get(
            "chunk_size", 1024
        )
        chunk_overlap = repo_overrides.get("chunk_overlap") or tenant_defaults.get(
            "chunk_overlap", 150
        )

        # Resolve vector index name for this repo
        vector_config = repo_config.get("vector_config", {})
        index_name = vector_config.get("index_name", f"vidx_tenant_{tenant_id}")

        # ── Step 2: Build per-task services ───────────────────────────────────
        # VectorStore and IngestionService are per-task — they need the
        # index_name from repo config which varies per repo/tenant

        # ── Provider selection from tenant ingestion_defaults ─────────────────
        # Tenant config can override the global embedding provider/model.
        # Currently OpenAI is the only provider — future: Azure OpenAI, Cohere etc.
        # Falls back to the global startup provider if not specified.
        ingestion_defaults = tenant_config.get("ingestion_defaults", {})
        tenant_embedding_model = ingestion_defaults.get("embedding_model")
        if (
            tenant_embedding_model
            and tenant_embedding_model != settings.EMBEDDING_MODEL
        ):
            try:
                from app.providers.embedding.openai_provider import (
                    OpenAIEmbeddingProvider,
                )

                task_embedding_provider = OpenAIEmbeddingProvider(
                    api_key=settings.OPENAI_API_KEY,
                    model=tenant_embedding_model,
                )
                bound_log.info(
                    "task.embedding_provider_override",
                    model=tenant_embedding_model,
                )
            except Exception as e:
                bound_log.warning(
                    "task.embedding_provider_override_failed",
                    error=str(e),
                    fallback=settings.EMBEDDING_MODEL,
                )
                task_embedding_provider = embedding_provider
        else:
            task_embedding_provider = embedding_provider  # use global default

        db = get_database()
        vector_store = MongoDBAtlasVectorStore(
            db=db,
            index_name=index_name,
            lc_embeddings=task_embedding_provider.as_langchain_embeddings(),
        )

        ingestion_service = IngestionService(
            embedding_provider=task_embedding_provider,
            vector_store=vector_store,
            document_repo=document_repo,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            loader_llm=ctx.get(
                "loader_llm"
            ),  # None if init failed — graceful degradation
        )

        tenant_metadata_schema = tenant_config.get("metadata_schema")

        # ── Step 3: Connect to data source ────────────────────────────────────
        source_type = repo_config.get("source_type", "local")
        connector_config = repo_config.get("connector_config", {})

        # Read state from repo config:
        #   delta_token   → SharePoint incremental scan
        #   last_run_at   → SQL/MongoDB delta fetch (WHERE updated_at > last_run_at)
        repo_state = repo_config.get("state", {})
        delta_token = repo_state.get("delta_token")

        last_run_at_raw = repo_state.get("last_successful_run_at")
        last_run_at = None
        if last_run_at_raw:
            if isinstance(last_run_at_raw, datetime):
                last_run_at = (
                    last_run_at_raw
                    if last_run_at_raw.tzinfo
                    else last_run_at_raw.replace(tzinfo=timezone.utc)
                )

        connector = get_connector(
            source_type=source_type,
            connector_config=connector_config,
            credentials=credentials,
            delta_token=delta_token,
            last_run_at=last_run_at,
        )
        await connector.connect()

        # ── Step 4: Stream and process documents ──────────────────────────────
        # Track source_ids seen in this run — used in Step 5 for deletion detection
        seen_source_ids: set[str] = set()

        bound_log.info("task.fetching_documents")

        async for delta_item in connector.fetch_documents():

            if delta_item.type == "deleted":
                # Connector explicitly reported this item as deleted
                # (only connectors with supports_native_deletes=True yield these,
                #  e.g. SharePoint delta API)
                #
                # Route through deduplicator first — only delete from vectors
                # if the document was actually ingested. Guards against SP reporting
                # deletions for files we never processed (unsupported type, filtered out).
                dedup_result = await ingestion_service._deduplicator.mark_deleted(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    source_id=delta_item.source_id,
                )
                if dedup_result.existing_doc:
                    deleted_chunks = await vector_store.delete_by_source(
                        source_id=delta_item.source_id,
                        tenant_id=tenant_id,
                        repo_id=repo_id,
                    )
                    await document_repo.mark_deleted(
                        tenant_id=tenant_id,
                        repo_id=repo_id,
                        source_id=delta_item.source_id,
                    )
                    stats.record_deletion(chunks_deleted=deleted_chunks)
                    bound_log.info("document.deleted", source_id=delta_item.source_id)
                continue

            # type == "upsert" — new or potentially changed document
            seen_source_ids.add(delta_item.source_id)
            raw_doc = delta_item.raw_doc

            result = await ingestion_service.process_document(
                raw_doc=raw_doc,
                tenant_id=tenant_id,
                repo_id=repo_id,
                job_id=job_id,
                tenant_metadata_schema=tenant_metadata_schema,
            )
            stats.record_document_result(result)

            if result.get("status") == "failed":
                await ctx["job_repo"].append_document_error(
                    job_id=job_id,
                    file_name=raw_doc.file_name,
                    error=result.get("error", "Unknown error"),
                )

        # ── Step 5: Detect implicit deletes ───────────────────────────────────
        # Connectors with supports_native_deletes=True (SharePoint) already yielded
        # explicit DeltaItem(type="deleted") in Step 4 — nothing more to do.
        #
        # Connectors without native delete support (local, SQL, MongoDB) need us
        # to compare what we saw this run vs what's in the registry.
        # Anything in the registry but NOT seen → deleted.
        if not connector.supports_native_deletes:
            deleted_ids = await ingestion_service._deduplicator.check_for_deletions(
                tenant_id=tenant_id,
                repo_id=repo_id,
                seen_source_ids=seen_source_ids,
            )
            for source_id in deleted_ids:
                deleted_chunks = await vector_store.delete_by_source(
                    source_id=source_id,
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                )
                await document_repo.mark_deleted(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    source_id=source_id,
                )
                stats.record_deletion(chunks_deleted=deleted_chunks)
                bound_log.info("document.deleted", source_id=source_id)

        # ── Step 6: Persist run state ─────────────────────────────────────────
        # SharePoint: save deltaLink for incremental next scan
        if connector.delta_token:
            await repo_config_repo.save_delta_token(repo_id, connector.delta_token)

        # SQL/MongoDB: save last_successful_run_at for delta fetch on next run
        # (WHERE updated_at > last_run_at)
        if source_type in ("sql", "mongodb"):
            await repo_config_repo.update_last_ingested(
                repo_id, datetime.now(timezone.utc)
            )
            bound_log.info("task.last_run_saved", source_type=source_type)

        await connector.disconnect()

        # ── Step 7: Mark job completed ────────────────────────────────────────
        await job_lifecycle.complete(job_id, stats)
        bound_log.info("task.completed", **stats.to_dict())
        return stats.to_dict()

    except (RepoConfigNotFoundError, ConnectorError) as e:
        # Job-level failure — can't continue at all
        bound_log.error("task.failed", error=str(e))
        await job_lifecycle.fail(job_id, str(e))
        raise  # re-raise so ARQ marks the task as failed

    except Exception as e:
        # Unexpected job-level failure
        bound_log.error("task.failed_unexpected", error=str(e))
        await job_lifecycle.fail(job_id, str(e))
        raise


# ── ARQ Worker Configuration ──────────────────────────────────────────────────


class WorkerSettings:
    """
    ARQ reads this class to configure the worker process.
    Start the worker with: arq app.worker.WorkerSettings
    """

    functions = [process_ingestion_task]
    on_startup = startup
    on_shutdown = shutdown

    redis_settings = RedisSettings.from_dsn(str(settings.REDIS_URI))
    queue_name = settings.QUEUE_NAME

    # How many tasks this worker runs concurrently
    # Keep at 5 — each task itself is async and handles its own concurrency
    max_jobs = 5

    # Hard timeout per task — 1 hour max for any single ingestion job
    job_timeout = 3600

    # Keep the task result in Redis for 1 hour after completion
    keep_result = 3600

    # Retry failed tasks up to 3 times with 30s delay
    max_tries = 3
