"""
MetadataEnricher — attaches rich metadata to every chunk.

Two layers:
  1. Standard metadata — always present on every chunk regardless of tenant
  2. Custom metadata  — tenant-defined fields extracted from path/properties

Phase 1: standard metadata only.
Phase 7: custom tenant metadata schema support.
"""

from datetime import datetime, timezone
from typing import Any

import structlog
from langchain_core.documents import Document

from app.connectors.base import RawDocument

log = structlog.get_logger(__name__)


class MetadataEnricher:

    def enrich(
        self,
        chunks: list[Document],
        raw_doc: RawDocument,
        tenant_id: str,
        repo_id: str,
        job_id: str,
        tenant_metadata_schema: dict | None = None,
    ) -> list[Document]:
        """
        Attach standard + custom metadata to every chunk.
        Mutates chunks in place and returns them.
        """
        total_chunks = len(chunks)
        now = datetime.now(timezone.utc)

        for i, chunk in enumerate(chunks):
            # ── Standard metadata — always present ────────────────────────────
            standard: dict[str, Any] = {
                # Tenant + job context
                "tenant_id": tenant_id,
                "repo_id": repo_id,
                "job_id": job_id,
                # Chunk position
                "chunk_index": i,
                "chunk_total": total_chunks,
                # File identity
                "file_name": raw_doc.file_name,
                "file_extension": (
                    raw_doc.file_name.rsplit(".", 1)[-1].lower()
                    if "." in raw_doc.file_name
                    else ""
                ),
                "mime_type": raw_doc.mime_type,
                "source_id": raw_doc.source_id,
                "content_hash": raw_doc.content_hash,
                # Location — full path and URL always populated
                "full_path": raw_doc.full_path,
                "source_url": raw_doc.source_url,
                # Temporal
                "last_modified_at_source": (
                    raw_doc.last_modified_at_source.isoformat()
                    if raw_doc.last_modified_at_source
                    else None
                ),
                "ingested_at": now.isoformat(),
                # Source-specific extras (e.g. sharepoint_item_id)
                **raw_doc.extra_metadata,
            }

            # Merge: standard fields take priority over loader-extracted metadata
            chunk.metadata = {**chunk.metadata, **standard}

            # ── Custom tenant metadata (Phase 7) ──────────────────────────────
            if tenant_metadata_schema:
                custom = self._extract_custom_fields(raw_doc, tenant_metadata_schema)
                # Custom fields do NOT override standard fields
                chunk.metadata = {**custom, **chunk.metadata}

        return chunks

    def _extract_custom_fields(
        self, raw_doc: RawDocument, schema: dict
    ) -> dict[str, Any]:
        """
        Phase 7: extract tenant-defined custom fields from document properties.
        Phase 1: returns empty dict — not yet implemented.
        """
        return {}
