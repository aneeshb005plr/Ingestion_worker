"""
DocumentPrefixBuilder — builds a document identity prefix for every chunk.

R6a: Document identity prefix
  Core problem:
    Chunks lose document identity when split.
    "Role: Primary | Name: Brad Jorgenson" has zero semantic
    connection to "Smart Pricing Tool owner" because "Smart Pricing Tool"
    never appears in that chunk.

  Solution:
    Prepend a short identity prefix to every chunk text BEFORE embedding.
    Not in metadata — IN the text, so the embedding captures it.

  Result:
    "[Smart Pricing Tool | XLOS | SPT Runbook | Contact Information]
     Role: Primary | Name: Brad Jorgenson..."

    Query: "Who is the owner of Smart Pricing Tool?"
    Now matches because "Smart Pricing Tool" is IN the chunk ✅

Design: fully tenant-agnostic, zero hardcoded field names
  Uses the fields the tenant already configured — in priority order:

  Priority 1 — extractable_fields (most curated)
    Tenant explicitly said: "LLM can extract these from questions"
    These are the most semantically meaningful fields
    e.g. ["application", "domain"]

  Priority 2 — filterable_fields (fallback)
    Tenant said: "users filter by these"
    Still meaningful, just not LLM-extractable
    e.g. ["department", "region", "access_group"]
    Used when extractable_fields is empty or not configured

  Priority 3 — all custom metadata (last resort)
    Tenant has no field config at all
    Use whatever MetadataEnricher extracted from path/properties
    Skip standard system fields (tenant_id, repo_id, job_id etc.)

  Always added (regardless of priority):
    file stem    — human readable document name, always meaningful
    section heading — h1/h2/h3 from chunk metadata, adds location context

  Skip values:
    "unknown", "general", "default", "none" — add noise, no signal

Prefix format:
  "[value1 | value2 | file stem | heading]"

  DocAssist (extractable: ["application","domain"]):
    "[Smart Pricing Tool | XLOS | SPT Runbook | Contact Information]"

  HR (filterable: ["department","region"], no extractable):
    "[Human Resources | US | HR 045 Annual Leave Policy]"

  Simple (no field config):
    "[FAQ | Getting Started Guide | Introduction]"

  Bare minimum (nothing at all):
    "[SPT Runbook]"

Position in pipeline:
  Step 4.5 — after MetadataEnricher (Step 4), before Embed (Step 5)
  MetadataEnricher runs first so all custom fields are in chunk.metadata
  when we read them here.
"""

from pathlib import Path
from langchain_core.documents import Document
import structlog

log = structlog.get_logger(__name__)

# Standard system fields added by MetadataEnricher — not meaningful for prefix
# These are infrastructure fields, not document content dimensions
_SYSTEM_FIELDS = {
    "tenant_id",
    "repo_id",
    "job_id",
    "chunk_index",
    "chunk_total",
    "file_name",
    "file_extension",
    "mime_type",
    "source_id",
    "content_hash",
    "full_path",
    "source_url",
    "last_modified_at_source",
    "ingested_at",
    "sharepoint_item_id",
    "sharepoint_drive_id",
    # heading keys — handled separately
    "h1",
    "h2",
    "h3",
    "h4",
}

# Values that add noise rather than signal
_SKIP_VALUES = {
    "unknown",
    "general",
    "default",
    "none",
    "null",
    "n/a",
    "na",
    "other",
    "misc",
    "miscellaneous",
    "true",
    "false",
}

# Max values to include in prefix — beyond this it gets too long
_MAX_PREFIX_VALUES = 3


class DocumentPrefixBuilder:
    """
    Builds and prepends document identity prefix to chunk text before embedding.
    Zero hardcoded field names — fully driven by tenant config.
    """

    def build_prefix(
        self,
        file_name: str,
        chunk_metadata: dict,
        extractable_fields: list[str],
        filterable_fields: list[str],
    ) -> str:
        """
        Build document identity prefix.

        Args:
            file_name:          Source file name e.g. "SPT_Runbook.docx"
            chunk_metadata:     Chunk metadata after MetadataEnricher ran
            extractable_fields: From repo retrieval_config.extractable_fields
                                [] if not configured
            filterable_fields:  From repo retrieval_config.filterable_fields
                                [] if not configured

        Returns:
            Formatted prefix e.g. "[Smart Pricing Tool | XLOS | SPT Runbook]"
            Empty string only if truly nothing available (extremely rare)
        """
        parts = []

        # ── Priority 1/2/3: Get values from metadata fields ───────────────────
        field_values = self._extract_field_values(
            metadata=chunk_metadata,
            extractable_fields=extractable_fields,
            filterable_fields=filterable_fields,
        )
        parts.extend(field_values[:_MAX_PREFIX_VALUES])

        # ── Always: file stem ─────────────────────────────────────────────────
        file_stem = self._clean_file_stem(file_name)
        if file_stem and file_stem not in parts:
            parts.append(file_stem)

        # ── Always: section heading ───────────────────────────────────────────
        heading = self._extract_heading(chunk_metadata)
        if heading and heading not in parts:
            parts.append(heading)

        if not parts:
            return ""

        return "[" + " | ".join(parts) + "]"

    def enrich_chunks(
        self,
        chunks: list[Document],
        file_name: str,
        extractable_fields: list[str],
        filterable_fields: list[str],
    ) -> list[Document]:
        """
        Prepend document identity prefix to every chunk's page_content.

        Called from ingestion_service.py at Step 4.5.
        Mutates chunks in place and returns them.
        """
        if not chunks:
            return chunks

        prefixed_count = 0
        for chunk in chunks:
            prefix = self.build_prefix(
                file_name=file_name,
                chunk_metadata=chunk.metadata,
                extractable_fields=extractable_fields,
                filterable_fields=filterable_fields,
            )
            if prefix:
                chunk.page_content = f"{prefix}\n{chunk.page_content}"
                prefixed_count += 1

        log.debug(
            "prefix_builder.done",
            file=file_name,
            total=len(chunks),
            prefixed=prefixed_count,
            extractable_fields=extractable_fields,
            filterable_fields=filterable_fields,
        )

        return chunks

    # ── Field extraction ──────────────────────────────────────────────────────

    def _extract_field_values(
        self,
        metadata: dict,
        extractable_fields: list[str],
        filterable_fields: list[str],
    ) -> list[str]:
        """
        Extract meaningful values using priority chain.

        Priority 1: extractable_fields  → most curated, use first
        Priority 2: filterable_fields   → fallback if no extractable
        Priority 3: all custom fields   → fallback if neither configured
        """

        # Priority 1 — extractable_fields
        if extractable_fields:
            return self._values_from_fields(metadata, extractable_fields)

        # Priority 2 — filterable_fields (minus access control fields)
        # Skip access_group/is_general — these are access control dimensions
        # not content dimensions. Adding them to prefix adds no retrieval signal.
        if filterable_fields:
            content_fields = [
                f
                for f in filterable_fields
                if f not in {"access_group", "is_general", "source_id"}
            ]
            return self._values_from_fields(metadata, content_fields)

        # Priority 3 — all custom metadata fields
        # Skip system fields, use whatever was extracted from path/properties
        return self._values_from_all_custom(metadata)

    def _values_from_fields(
        self,
        metadata: dict,
        fields: list[str],
    ) -> list[str]:
        """Extract non-empty meaningful values for given field names."""
        values = []
        for field in fields:
            value = metadata.get(field)
            if value and self._is_meaningful(str(value)):
                values.append(str(value).strip())
        return values

    def _values_from_all_custom(self, metadata: dict) -> list[str]:
        """
        Fallback: extract values from all non-system metadata fields.
        Used when tenant has no extractable_fields or filterable_fields.
        """
        values = []
        for key, value in metadata.items():
            if key in _SYSTEM_FIELDS:
                continue
            if value and self._is_meaningful(str(value)):
                values.append(str(value).strip())
            if len(values) >= _MAX_PREFIX_VALUES:
                break
        return values

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_heading(self, metadata: dict) -> str:
        """
        Extract most specific section heading.
        h2 preferred (more specific than h1), then h1, then h3.
        Skips headings longer than 80 chars (likely paragraph text).
        """
        for key in ("h2", "h1", "h3"):
            val = metadata.get(key)
            if val and self._is_meaningful(str(val)):
                cleaned = str(val).strip()
                if len(cleaned) <= 80:
                    return cleaned
        return ""

    @staticmethod
    def _clean_file_stem(file_name: str) -> str:
        """
        Convert file name to human readable stem.
          "SPT_Runbook.docx"              → "SPT Runbook"
          "Annual-Leave-Policy-v2.pdf"    → "Annual Leave Policy v2"
          "HR045_Policy.pdf"              → "HR045 Policy"
        """
        stem = Path(file_name).stem
        stem = stem.replace("_", " ").replace("-", " ")
        stem = " ".join(stem.split())  # collapse multiple spaces
        return stem if len(stem) > 1 else ""

    @staticmethod
    def _is_meaningful(value: str) -> bool:
        """
        Returns True if value adds retrieval signal.
        Filters out generic/noise/system values.
        """
        v = value.strip().lower()
        if not v:
            return False
        if v in _SKIP_VALUES:
            return False
        if len(v) <= 1:
            return False
        if v.isdigit():
            return False
        return True
