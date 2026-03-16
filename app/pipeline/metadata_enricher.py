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
        Extract tenant-defined custom metadata fields from document properties.

        Schema format (matches MetadataSchema in orchestrator schemas/tenant.py):
          {
            "custom_fields": [
              {
                "field_name": "department",
                "source": "path_segment",   # extract from full_path
                "path_segment_index": 1,    # e.g. /finance/Q3.pdf → "finance"
                "default": "unknown"
              },
              {
                "field_name": "doc_type",
                "source": "file_name",      # extract from file_name prefix/suffix
                "default": "document"
              },
              {
                "field_name": "region",
                "source": "static",         # same value for all docs in this tenant
                "default": "APAC"
              }
            ]
          }
        """
        custom: dict[str, Any] = {}
        custom_fields = schema.get("custom_fields", [])

        for field_def in custom_fields:
            field_name = field_def.get("field_name")
            source = field_def.get("source")
            default = field_def.get("default", "unknown")

            if not field_name or not source:
                continue

            try:
                if source == "path_segment":
                    # Extract a segment from the full_path by index
                    # e.g. full_path="/finance/reports/Q3.pdf", index=1 → "reports"
                    index = field_def.get("path_segment_index", 0)
                    segments = [s for s in raw_doc.full_path.split("/") if s]
                    # Exclude filename (last segment)
                    path_parts = segments[:-1] if len(segments) > 1 else segments
                    value = path_parts[index] if index < len(path_parts) else default

                elif source == "file_name":
                    # Use the file stem (name without extension)
                    value = (
                        raw_doc.file_name.rsplit(".", 1)[0]
                        if "." in raw_doc.file_name
                        else raw_doc.file_name
                    )

                elif source == "static":
                    # Same value for every document under this tenant
                    value = default

                else:
                    log.warning(
                        "metadata.unknown_source",
                        field=field_name,
                        source=source,
                    )
                    value = default

                custom[field_name] = value

            except Exception as e:
                log.warning(
                    "metadata.custom_field_failed",
                    field=field_name,
                    source=source,
                    error=str(e),
                )
                custom[field_name] = default

        return custom
