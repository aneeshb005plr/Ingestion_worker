"""
IngestionService — runs the full pipeline for a single document.

Pipeline (identical for ALL source types):
  1. Dedup check   → skip if unchanged
  2. Load          → RawDocument → Markdown (via LoaderFactory)
                     File:       Docling / MarkItDown / TextLoader
                     Structured: SQLLoader / MongoDBLoader
  3. Chunk         → Markdown → text segments
  4. Enrich        → attach metadata to every chunk
  4.5 Prefix       → prepend document identity to chunk text (R6a)
  5. Embed         → text → float vectors
  6. Delete        → remove old chunks if this is an update
  7. Store         → vectors → MongoDB Atlas
  8. Register      → update ingested_documents registry

The LoaderFactory handles routing — ingestion_service never checks content_type.
Adding a new source type = add a loader. Zero changes here.
"""

import asyncio
from typing import Optional

import structlog

from app.connectors.base import RawDocument
from app.loaders.factory import LoaderFactory
from app.pipeline.chunk_contextualiser import ChunkContextualiser
from app.pipeline.chunker import Chunker
from app.pipeline.document_prefix_builder import DocumentPrefixBuilder
from app.pipeline.metadata_enricher import MetadataEnricher
from app.pipeline.table_converter import TableConverter
from app.pipeline.deduplicator import Deduplicator
from app.providers.embedding.base import BaseEmbeddingProvider
from app.providers.vectorstore.base import BaseVectorStore, VectorChunk
from app.repositories.document_repo import DocumentRepository
from app.core.exceptions import UnsupportedDocumentTypeError, DocumentParsingError

log = structlog.get_logger(__name__)


class IngestionService:

    def __init__(
        self,
        embedding_provider: BaseEmbeddingProvider,
        vector_store: BaseVectorStore,
        document_repo: DocumentRepository,
        chunk_size: int = 1024,
        chunk_overlap: int = 150,
        loader_llm=None,
        contextual_enrichment_enabled: bool = False,
        contextual_enrichment_mode: str = "all_chunks",
        contextual_enrichment_concurrency: int = 3,
    ) -> None:
        self._embedder = embedding_provider
        self._vector_store = vector_store
        self._document_repo = document_repo

        # Pass llm to LoaderFactory — used only when complexity detected
        # (scanned PDFs, embedded images, nested tables)
        self._loader_factory = LoaderFactory(llm=loader_llm)
        self._chunker = Chunker(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        self._table_converter = TableConverter()
        self._enricher = MetadataEnricher()
        self._deduplicator = Deduplicator(document_repo)
        self._prefix_builder = DocumentPrefixBuilder()

        # Contextual enrichment — LLM-based chunk context prepend
        # Uses same LLM as loader (loader_llm) — reuses tenant api_config.
        # Only created when enabled=True AND llm is available.
        # Graceful degradation: disabled or no LLM → _contextualiser is None
        #   → Step 3.5 is silently skipped
        self._contextual_enrichment_mode = contextual_enrichment_mode
        self._contextual_enrichment_concurrency = contextual_enrichment_concurrency
        self._contextualiser: ChunkContextualiser | None = (
            ChunkContextualiser(llm=loader_llm)
            if contextual_enrichment_enabled and loader_llm is not None
            else None
        )
        if self._contextualiser is not None:
            log.debug(
                "ingestion_service.contextualiser_ready",
                mode=contextual_enrichment_mode,
                concurrency=contextual_enrichment_concurrency,
            )

    # ── Deduplicator exposed for worker Step 4/5 access ───────────────────────
    # Worker needs _deduplicator for deleted item handling and implicit delete detection.
    # Exposing via property keeps internals private but avoids name-mangled access.
    @property
    def deduplicator(self) -> "Deduplicator":
        return self._deduplicator

    async def process_document(
        self,
        raw_doc: RawDocument,
        tenant_id: str,
        repo_id: str,
        job_id: str,
        tenant_metadata_schema: dict | None = None,
        extractable_fields: list[str] | None = None,
        filterable_fields: list[str] | None = None,
        embed_semaphore: Optional[asyncio.Semaphore] = None,
    ) -> dict:
        """
        Run the full pipeline for one document or structured record.

        Args:
            embed_semaphore: Optional shared semaphore from the worker that
                             limits concurrent OpenAI embedding calls across
                             all documents processing simultaneously.
                             If None, embedding runs without rate limit control
                             (fine for single-document or low-concurrency use).

        Returns:
          {"status": "new"|"updated"|"skipped"|"failed", "chunks": int, "error"?: str}
        """
        bound_log = log.bind(
            file=raw_doc.file_name,
            source_type=raw_doc.content_type,
            tenant=tenant_id,
            repo=repo_id,
        )

        # ── Step 1: Dedup check ───────────────────────────────────────────────
        dedup = await self._deduplicator.check(
            tenant_id=tenant_id,
            repo_id=repo_id,
            source_id=raw_doc.source_id,
            content_hash=raw_doc.content_hash,
            last_modified_at_source=raw_doc.last_modified_at_source,
        )

        if dedup.decision == "unchanged":
            bound_log.debug("document.skipped", reason="unchanged")
            return {"status": "skipped", "chunks": 0}

        try:
            # ── Step 2: Load → Markdown ───────────────────────────────────────
            # LoaderFactory selects the right loader for this content_type.
            # File:       uses raw_doc.mime_type → PDFLoader / DocxLoader / TextLoader
            # Structured: uses raw_doc.structured_source_type → SQLLoader / MongoDBLoader
            # No special cases here — factory handles all routing.
            loader = self._loader_factory.get_loader(raw_doc)
            parsed = await loader.load(raw_doc)

            # Convert pipe tables to prose before chunking
            # Improves embedding quality for key-value tables like
            # "Application Owner | Brad Jorgenson | +1 214-984-9828"
            parsed.markdown_text = self._table_converter.convert(parsed.markdown_text)

            if not parsed.markdown_text.strip():
                bound_log.warning("document.empty", reason="no text extracted")
                return {"status": "skipped", "chunks": 0}

            # ── Step 3: Chunk ─────────────────────────────────────────────────
            lc_chunks = self._chunker.split(parsed.markdown_text)

            if not lc_chunks:
                bound_log.warning("document.empty", reason="no chunks produced")
                return {"status": "skipped", "chunks": 0}

            # ── Step 3.5: Contextual enrichment ──────────────────────────────
            # Prepend LLM-generated 2-sentence context to each chunk.
            # Based on Anthropic Contextual Retrieval — reduces retrieval
            # failures by 49% for structured documents (tables, contacts etc.)
            # Only runs if contextual_enrichment_enabled=True AND LLM available.
            if self._contextualiser is not None:
                context_semaphore = asyncio.Semaphore(
                    self._contextual_enrichment_concurrency
                )
                lc_chunks = await self._contextualiser.enrich(
                    chunks=lc_chunks,
                    file_name=raw_doc.file_name,
                    mode=self._contextual_enrichment_mode,
                    semaphore=context_semaphore,
                )
                bound_log.debug(
                    "document.contextual_enrichment_done",
                    chunks=len(lc_chunks),
                    mode=self._contextual_enrichment_mode,
                )

            # ── Step 4: Enrich metadata ───────────────────────────────────────
            self._enricher.enrich(
                chunks=lc_chunks,
                raw_doc=raw_doc,
                tenant_id=tenant_id,
                repo_id=repo_id,
                job_id=job_id,
                tenant_metadata_schema=tenant_metadata_schema,
            )

            # ── Step 4.5: Document identity prefix ───────────────────────────
            # R6a: Prepend document identity to every chunk text BEFORE embedding.
            # Runs AFTER metadata enrichment so all custom fields are available.
            #
            # Why needed:
            #   Chunks have no application name in text after splitting.
            #   Query "SPT owner" needs "Smart Pricing Tool" in chunk text
            #   to match via embedding — filter scopes the search but
            #   embedding still needs the vocabulary bridge.
            #
            # "[Smart Pricing Tool | XLOS | SPT Runbook | Contact Information]
            #  Brad Jorgenson is the primary application owner..."
            lc_chunks = self._prefix_builder.enrich_chunks(
                chunks=lc_chunks,
                file_name=raw_doc.file_name,
                extractable_fields=extractable_fields or [],
                filterable_fields=filterable_fields or [],
            )
            bound_log.debug(
                "document.prefix_applied",
                chunks=len(lc_chunks),
                file=raw_doc.file_name,
                used_extractable=bool(extractable_fields),
                used_filterable=bool(filterable_fields and not extractable_fields),
            )

            # ── Step 5: Embed ─────────────────────────────────────────────────
            # Acquire shared semaphore if provided — limits total concurrent
            # OpenAI calls across all documents processing simultaneously.
            texts = [chunk.page_content for chunk in lc_chunks]
            if embed_semaphore is not None:
                async with embed_semaphore:
                    embeddings = await self._embedder.embed_documents(texts)
            else:
                embeddings = await self._embedder.embed_documents(texts)

            vector_chunks = [
                VectorChunk(
                    text=chunk.page_content,
                    embedding=embedding,
                    metadata=chunk.metadata,
                )
                for chunk, embedding in zip(lc_chunks, embeddings)
            ]

            # ── Step 6: Delete old chunks if updated ──────────────────────────
            chunks_deleted = 0
            if dedup.decision == "changed":
                chunks_deleted = await self._vector_store.delete_by_source(
                    source_id=raw_doc.source_id,
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                )
                bound_log.info("document.old_chunks_deleted", deleted=chunks_deleted)

            # ── Step 7: Store vectors ─────────────────────────────────────────
            await self._vector_store.add_documents(
                chunks=vector_chunks,
                tenant_id=tenant_id,
                repo_id=repo_id,
            )

            # ── Step 8: Update registry ───────────────────────────────────────
            await self._document_repo.upsert(
                tenant_id=tenant_id,
                repo_id=repo_id,
                source_id=raw_doc.source_id,
                job_id=job_id,
                file_name=raw_doc.file_name,
                full_path=raw_doc.full_path,
                source_url=raw_doc.source_url,
                mime_type=raw_doc.mime_type
                or raw_doc.structured_source_type
                or "unknown",
                content_hash=raw_doc.content_hash,
                last_modified_at_source=raw_doc.last_modified_at_source,
                chunk_count=len(vector_chunks),
            )

            status = "updated" if dedup.decision == "changed" else "new"
            bound_log.info(
                "document.processed", status=status, chunks=len(vector_chunks)
            )
            return {
                "status": status,
                "chunks": len(vector_chunks),
                "chunks_deleted": chunks_deleted,  # 0 for new, >0 for updated
            }

        except UnsupportedDocumentTypeError as e:
            bound_log.warning(
                "document.skipped", reason="unsupported_type", error=str(e)
            )
            return {"status": "skipped", "chunks": 0}

        except DocumentParsingError as e:
            bound_log.warning("document.failed", reason="parsing_error", error=str(e))
            return {"status": "failed", "chunks": 0, "error": str(e)}

        except Exception as e:
            bound_log.error("document.failed", reason="unexpected_error", error=str(e))
            return {"status": "failed", "chunks": 0, "error": str(e)}
