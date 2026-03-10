"""
IngestionService — runs the full pipeline for a single document.

Pipeline (identical for ALL source types):
  1. Dedup check   → skip if unchanged
  2. Load          → RawDocument → Markdown (via LoaderFactory)
                     File:       Docling / MarkItDown / TextLoader
                     Structured: SQLLoader / MongoDBLoader
  3. Chunk         → Markdown → text segments
  4. Enrich        → attach metadata to every chunk
  5. Embed         → text → float vectors
  6. Delete        → remove old chunks if this is an update
  7. Store         → vectors → MongoDB Atlas
  8. Register      → update ingested_documents registry

The LoaderFactory handles routing — ingestion_service never checks content_type.
Adding a new source type = add a loader. Zero changes here.
"""

import structlog

from app.connectors.base import RawDocument
from app.loaders.factory import LoaderFactory
from app.pipeline.chunker import Chunker
from app.pipeline.metadata_enricher import MetadataEnricher
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
    ) -> None:
        self._embedder = embedding_provider
        self._vector_store = vector_store
        self._document_repo = document_repo

        # Pass llm to LoaderFactory — used only when complexity detected
        # (scanned PDFs, embedded images, nested tables)
        self._loader_factory = LoaderFactory(llm=loader_llm)
        self._chunker = Chunker(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        self._enricher = MetadataEnricher()
        self._deduplicator = Deduplicator(document_repo)

    async def process_document(
        self,
        raw_doc: RawDocument,
        tenant_id: str,
        repo_id: str,
        job_id: str,
        tenant_metadata_schema: dict | None = None,
    ) -> dict:
        """
        Run the full pipeline for one document or structured record.

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

            if not parsed.markdown_text.strip():
                bound_log.warning("document.empty", reason="no text extracted")
                return {"status": "skipped", "chunks": 0}

            # ── Step 3: Chunk ─────────────────────────────────────────────────
            lc_chunks = self._chunker.split(parsed.markdown_text)

            if not lc_chunks:
                bound_log.warning("document.empty", reason="no chunks produced")
                return {"status": "skipped", "chunks": 0}

            # ── Step 4: Enrich metadata ───────────────────────────────────────
            self._enricher.enrich(
                chunks=lc_chunks,
                raw_doc=raw_doc,
                tenant_id=tenant_id,
                repo_id=repo_id,
                job_id=job_id,
                tenant_metadata_schema=tenant_metadata_schema,
            )

            # ── Step 5: Embed ─────────────────────────────────────────────────
            texts = [chunk.page_content for chunk in lc_chunks]
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
