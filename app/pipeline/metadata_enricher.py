"""
MetadataEnricher — attaches rich metadata to every chunk.

Two layers:
  1. Standard metadata — always present on every chunk regardless of tenant
  2. Custom metadata   — fields extracted per document using metadata_schema

Custom field resolution (resolved by worker BEFORE calling enricher):
  Priority:
    repo.metadata_schema   → takes priority (repo-specific structure)
    tenant.metadata_schema → fallback (shared default)
    None                   → no custom metadata

  Worker resolves priority and passes ONE effective schema here.
  No merging or override logic needed in enricher — keeps it simple.

Field sources supported:
  path_segment:        Extract folder name at path_segment_index
  file_name:           Use file stem without extension
  static:              Same value for all docs (via default)
  path_segment_equals: Boolean flag based on path segment match
  computed:            Derive from other fields via formula
                       e.g. "{application}::{access_group}" → "SPT::general"
                       computed fields must be defined LAST in custom_fields list
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

        Args:
            chunks:                   LangChain Document chunks from chunker
            raw_doc:                  Original document with path, content, etc.
            tenant_id:                Tenant identifier
            repo_id:                  Repo identifier
            job_id:                   Current ingestion job ID
            tenant_metadata_schema:   Effective schema — already resolved by worker.
                                      Priority: repo.metadata_schema > tenant.metadata_schema
                                      Passed as single resolved schema — no merging needed here.
        """
        total_chunks = len(chunks)
        now = datetime.now(timezone.utc)

        # Schema is already resolved by worker (repo > tenant priority)
        # No merging needed — use directly
        effective_schema = tenant_metadata_schema

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
                # Location
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

            # Standard fields take priority over loader-extracted metadata
            chunk.metadata = {**chunk.metadata, **standard}

            # ── Custom metadata ───────────────────────────────────────────────
            if effective_schema:
                custom = self._extract_custom_fields(raw_doc, effective_schema)
                # Custom fields do NOT override standard fields
                chunk.metadata = {**custom, **chunk.metadata}

        return chunks

    # ── Custom field extraction ───────────────────────────────────────────────

    def _extract_custom_fields(
        self, raw_doc: RawDocument, schema: dict
    ) -> dict[str, Any]:
        """
        Extract custom metadata fields from document properties using effective schema.

        Schema format (matches MetadataSchema in orchestrator schemas/tenant.py):
          {
            "custom_fields": [
              {
                "field_name": "domain",
                "source": "path_segment",
                "path_segment_index": 0,
                "default": "general"
              },
              {
                "field_name": "is_general",
                "source": "path_segment_equals",
                "path_segment_index": 0,
                "match_value": "general",
                "true_value": "true",
                "false_value": "false"
              },
              {
                "field_name": "region",
                "source": "static",
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
                    index = field_def.get("path_segment_index", 0)
                    segments = [s for s in raw_doc.full_path.split("/") if s]
                    path_parts = segments[:-1] if len(segments) > 1 else segments
                    value = path_parts[index] if index < len(path_parts) else default

                elif source == "file_name":
                    value = (
                        raw_doc.file_name.rsplit(".", 1)[0]
                        if "." in raw_doc.file_name
                        else raw_doc.file_name
                    )

                elif source == "static":
                    value = default

                elif source == "path_segment_equals":
                    index = field_def.get("path_segment_index", 0)
                    match_value = field_def.get("match_value", "")
                    true_value = field_def.get("true_value", "true")
                    false_value = field_def.get("false_value", "false")
                    segments = [s for s in raw_doc.full_path.split("/") if s]
                    path_parts = segments[:-1] if len(segments) > 1 else segments
                    segment = path_parts[index] if index < len(path_parts) else ""
                    value = true_value if segment == match_value else false_value

                elif source == "computed":
                    # Derive value from other already-extracted fields
                    # using a formula template string.
                    #
                    # formula: "{application}::{access_group}"
                    # custom so far: { application: "SPT", access_group: "general" }
                    # result: "Smart Pricing Tool::general"
                    #
                    # IMPORTANT: computed fields must be defined AFTER
                    # the fields they reference in custom_fields list.
                    # We use `custom` dict built so far — earlier fields available.
                    formula = field_def.get("formula", "")
                    if not formula:
                        value = default
                    else:
                        try:
                            # Replace {field_name} with extracted value
                            # Falls back to default if referenced field missing
                            value = formula
                            import re

                            placeholders = re.findall(r"\{(\w+)\}", formula)
                            for placeholder in placeholders:
                                field_value = custom.get(placeholder, "")
                                if not field_value or field_value == "unknown":
                                    # Referenced field missing → use default
                                    value = default
                                    break
                                value = value.replace(f"{{{placeholder}}}", field_value)
                        except Exception:
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
