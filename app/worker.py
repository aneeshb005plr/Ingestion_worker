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

import asyncio
from datetime import datetime, timezone

import structlog
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.api_config import resolve_api_config
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
from app.notifications.notification_service import NotificationService

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

    # 3. Embedding provider — one OpenAI client for the whole worker process.
    # batch_size and concurrency use defaults here — overridden per-task
    # if tenant config specifies different values.
    ctx["embedding_provider"] = OpenAIEmbeddingProvider(
        api_key=settings.OPENAI_API_KEY,
        model=settings.EMBEDDING_MODEL,
        batch_size=512,
    )

    # 4. LLM for loader enrichment (Vision OCR, image descriptions, nested table cleanup)
    # Built per-task from tenant api_config — different tenants may use different
    # models (gpt-4o, gpt-4.1-mini etc.) and different API keys/endpoints.
    # ctx["loader_llm"] intentionally not set here — resolved per task below.

    # 5. Job lifecycle service
    ctx["job_lifecycle"] = JobLifecycleService(ctx["job_repo"])
    ctx["notification_service"] = NotificationService()

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
    trigger: str = "manual",
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
      9. Send notifications (email/webhook) if configured

    Args:
      ctx       : ARQ worker context (shared across all tasks in this process)
      job_id    : UUID of the job record in MongoDB
      repo_id   : ID of the source_repository to ingest
      tenant_id : ID of the tenant that owns this repo
      trigger   : "manual" | "scheduled" — passed from job_service at enqueue time

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
        document_concurrency = repo_overrides.get(
            "document_concurrency"
        ) or tenant_defaults.get("document_concurrency", 10)
        embedding_batch_size = tenant_defaults.get("embedding_batch_size", 512)
        embedding_concurrency = tenant_defaults.get("embedding_concurrency", 5)

        # Contextual enrichment config
        # repo override takes precedence over tenant default
        tenant_enrichment_enabled = tenant_defaults.get(
            "contextual_enrichment_enabled", False
        )
        repo_enrichment_override = repo_overrides.get(
            "contextual_enrichment_enabled", None
        )
        contextual_enrichment_enabled = (
            repo_enrichment_override
            if repo_enrichment_override is not None
            else tenant_enrichment_enabled
        )
        contextual_enrichment_mode = tenant_defaults.get(
            "contextual_enrichment_mode", "all_chunks"
        )
        contextual_enrichment_concurrency = tenant_defaults.get(
            "contextual_enrichment_concurrency", 3
        )

        # Resolve vector index name and collection name for this repo
        vector_config = repo_config.get("vector_config", {})
        index_name = vector_config.get("index_name", f"vidx_repo_{repo_id}")
        collection_name = vector_config.get("collection_name", "vector_store")

        # ── Step 2: Build per-task services ───────────────────────────────────
        # VectorStore and IngestionService are per-task — they need the
        # index_name from repo config which varies per repo/tenant

        # ── Resolve per-tenant API config ─────────────────────────────────────
        # embedding_model  → from ingestion_defaults (HOW to ingest)
        # api_key/base_url → from api_config (WHERE/WHO — credentials + endpoint)
        # llm_model        → from api_config (used by retrieval service)
        tenant_api_cfg = await resolve_api_config(
            tenant_api_config=tenant_config.get("api_config"),
            tenant_ingestion_defaults=tenant_defaults,
        )

        if tenant_api_cfg.needs_custom_provider:
            try:
                task_embedding_provider = OpenAIEmbeddingProvider(
                    api_key=tenant_api_cfg.api_key,
                    model=tenant_api_cfg.embedding_model,
                    batch_size=embedding_batch_size,
                    base_url=tenant_api_cfg.base_url,
                )
                bound_log.info(
                    "task.embedding_provider_resolved",
                    model=tenant_api_cfg.embedding_model,
                    tenant_key=tenant_api_cfg.is_tenant_key,
                    custom_base_url=tenant_api_cfg.base_url is not None,
                )
            except Exception as e:
                bound_log.warning(
                    "task.embedding_provider_fallback",
                    error=str(e),
                    fallback=settings.EMBEDDING_MODEL,
                )
                task_embedding_provider = embedding_provider
        else:
            task_embedding_provider = embedding_provider  # reuse global default

        # ── Per-tenant LLM for Vision / loader enrichment ─────────────────────
        # Uses tenant llm_model + api_key + base_url from api_config.
        # Same model used for: scanned PDF OCR, image descriptions, table cleanup.
        # Built per-task so each tenant uses their own key and model.
        # Graceful degradation: if init fails, loaders run in library-only mode.
        try:
            from langchain_openai import ChatOpenAI

            lc_kwargs = dict(
                model=tenant_api_cfg.llm_model,
                temperature=0,
                max_tokens=4096,
                api_key=tenant_api_cfg.api_key,
            )
            if tenant_api_cfg.base_url:
                lc_kwargs["base_url"] = tenant_api_cfg.base_url
            task_loader_llm = ChatOpenAI(**lc_kwargs)
            bound_log.info(
                "task.loader_llm_resolved",
                model=tenant_api_cfg.llm_model,
                tenant_key=tenant_api_cfg.is_tenant_key,
            )
        except Exception as e:
            bound_log.warning(
                "task.loader_llm_failed",
                error=str(e),
                message="Vision/LLM enrichment disabled for this task",
            )
            task_loader_llm = None

        db = get_database()
        vector_store = MongoDBAtlasVectorStore(
            db=db,
            index_name=index_name,
            lc_embeddings=task_embedding_provider.as_langchain_embeddings(),
            collection_name=collection_name,
        )

        ingestion_service = IngestionService(
            embedding_provider=task_embedding_provider,
            vector_store=vector_store,
            document_repo=document_repo,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            loader_llm=task_loader_llm,
            contextual_enrichment_enabled=contextual_enrichment_enabled,
            contextual_enrichment_mode=contextual_enrichment_mode,
            contextual_enrichment_concurrency=contextual_enrichment_concurrency,
        )

        # ── Resolve metadata_schema — repo level takes priority ───────────────
        # Priority:
        #   1. repo.metadata_schema     → repo-specific structure (new)
        #   2. tenant.metadata_schema   → tenant default (backward compatible)
        #   3. None                     → no custom metadata extraction
        #
        # repo_metadata_overrides kept for backward compatibility
        # but repo.metadata_schema is the preferred approach going forward.
        repo_metadata_schema = repo_config.get("metadata_schema")
        tenant_metadata_schema = tenant_config.get("metadata_schema")
        repo_metadata_overrides = repo_overrides.get("metadata_overrides")

        # Use repo schema if defined, otherwise fall back to tenant schema
        effective_metadata_schema = repo_metadata_schema or tenant_metadata_schema

        bound_log.debug(
            "task.metadata_schema_resolved",
            source="repo" if repo_metadata_schema else "tenant",
            has_schema=effective_metadata_schema is not None,
        )

        # Retrieval config — used by DocumentPrefixBuilder (R6a)
        # Tells prefix builder which fields are meaningful for this repo
        retrieval_config = repo_config.get("retrieval_config", {})
        extractable_fields = retrieval_config.get("extractable_fields", [])
        filterable_fields = retrieval_config.get("filterable_fields", [])

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

        connector = await get_connector(
            source_type=source_type,
            connector_config=connector_config,
            credentials=credentials,
            delta_token=delta_token,
            last_run_at=last_run_at,
        )
        await connector.connect()

        # ── Step 4: Stream and process documents (concurrent) ────────────────
        # Semaphore limits how many documents process simultaneously.
        # Deleted items are handled inline (synchronously) — no concurrency needed.
        # Upsert items are dispatched to a TaskGroup, bounded by the semaphore.
        seen_source_ids: set[str] = set()

        # stats.record_document_result and stats.record_deletion must be
        # thread-safe. JobLifecycleStats uses simple counters — asyncio is
        # single-threaded so concurrent coroutines are safe without locks.
        # doc_semaphore  — max documents processing simultaneously
        # embed_semaphore — max concurrent OpenAI embedding calls across ALL
        #                   documents. Shared here so all 10 concurrent docs
        #                   compete for the same embedding budget, preventing
        #                   rate limit floods.
        doc_semaphore = asyncio.Semaphore(document_concurrency)
        embed_semaphore = asyncio.Semaphore(embedding_concurrency)

        bound_log.info(
            "task.fetching_documents",
            document_concurrency=document_concurrency,
            embedding_concurrency=embedding_concurrency,
            embedding_batch_size=embedding_batch_size,
        )

        async def process_upsert(raw_doc) -> None:
            """
            Process one upserted document under the doc semaphore.
            Embedding step acquires the shared embed_semaphore so total
            concurrent OpenAI calls stay within embedding_concurrency
            regardless of how many docs are processing simultaneously.
            """
            async with doc_semaphore:
                result = await ingestion_service.process_document(
                    raw_doc=raw_doc,
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    job_id=job_id,
                    tenant_metadata_schema=effective_metadata_schema,
                    repo_metadata_overrides=repo_metadata_overrides,
                    extractable_fields=extractable_fields,
                    filterable_fields=filterable_fields,
                    embed_semaphore=embed_semaphore,
                )
                stats.record_document_result(result)
                if result.get("status") == "failed":
                    await ctx["job_repo"].append_document_error(
                        job_id=job_id,
                        file_name=raw_doc.file_name,
                        error=result.get("error", "Unknown error"),
                    )

        async with asyncio.TaskGroup() as tg:
            async for delta_item in connector.fetch_documents():

                if delta_item.type == "deleted":
                    # Deleted items handled synchronously — no concurrency needed.
                    # Route through deduplicator: only delete if we actually ingested it.
                    dedup_result = await ingestion_service.deduplicator.mark_deleted(
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
                        bound_log.info(
                            "document.deleted", source_id=delta_item.source_id
                        )
                    continue

                # Upsert — dispatch to TaskGroup (bounded by semaphore)
                seen_source_ids.add(delta_item.source_id)
                tg.create_task(process_upsert(delta_item.raw_doc))

        # TaskGroup awaits all tasks before exiting — all documents processed here

        # ── Step 5: Detect implicit deletes ───────────────────────────────────
        # SharePoint yields explicit deleted items in Step 4 — nothing more to do.
        # Local, SQL, MongoDB compare seen_source_ids vs registry.
        if not connector.supports_native_deletes:
            deleted_ids = await ingestion_service.deduplicator.check_for_deletions(
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
            bound_log.info(
                "document.deleted",
                source_id=source_id,
                chunks_deleted=deleted_chunks,
            )

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

        # ── Step 8: Send notifications ────────────────────────────────────
        notification_config = repo_config.get("notification_config", {})
        notification_service: NotificationService = ctx["notification_service"]
        status = "partial" if stats.has_failures else "completed"
        await notification_service.notify(
            notification_config=notification_config,
            job_id=job_id,
            tenant_id=tenant_id,
            repo_id=repo_id,
            repo_name=repo_config.get("name", repo_id),
            trigger=trigger,
            status=status,
            stats=stats,
        )
        return stats.to_dict()

    except (RepoConfigNotFoundError, ConnectorError) as e:
        # Job-level failure — can't continue at all
        bound_log.error("task.failed", error=str(e))
        await job_lifecycle.fail(job_id, str(e))
        # Notify on failure
        notification_config = (
            repo_config.get("notification_config", {}) if repo_config else {}
        )
        await ctx["notification_service"].notify(
            notification_config=notification_config,
            job_id=job_id,
            tenant_id=tenant_id,
            repo_id=repo_id,
            repo_name=repo_config.get("name", repo_id) if repo_config else repo_id,
            trigger=trigger,
            status="failed",
            stats=stats,
            error=str(e),
        )
        raise  # re-raise so ARQ marks the task as failed

    except Exception as e:
        # Unexpected job-level failure
        bound_log.error("task.failed_unexpected", error=str(e))
        await job_lifecycle.fail(job_id, str(e))
        notification_config = (
            repo_config.get("notification_config", {}) if repo_config else {}
        )
        await ctx["notification_service"].notify(
            notification_config=notification_config,
            job_id=job_id,
            tenant_id=tenant_id,
            repo_id=repo_id,
            repo_name=repo_config.get("name", repo_id) if repo_config else repo_id,
            trigger=trigger,
            status="failed",
            stats=stats,
            error=str(e),
        )
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
    if settings.REDIS_PASSWORD:
        redis_settings.password = settings.REDIS_PASSWORD
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
