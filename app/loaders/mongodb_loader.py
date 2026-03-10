"""
MongoDBLoader — converts a MongoDB document (dict) into structured Markdown.

Input:  RawDocument with raw_data = {"field1": "val1", "nested": {...}, ...}
Output: ParsedDocument with clean Markdown

Markdown format produced:
  ## {collection} — {document_id}

  ### Top-level fields
  - **field1**: val1
  - **field2**: val2

  ### nested_object
  - **key1**: value1
  - **key2**: value2

Why nested sections instead of a flat table?
  MongoDB documents are often deeply nested. Flattening to a table loses
  the structure. Representing nested objects as ## sub-sections means:
  - The chunker can split on section headers if the document is large
  - Each section header carries context (collection name, nested object name)
  - Retrieval: "what is the shipping address of order O-123?" matches
    the address sub-section cleanly

The MongoDB connector puts collection and document_id in extra_metadata.
"""

import json
import structlog

from app.loaders.base import BaseLoader, ParsedDocument
from app.connectors.base import RawDocument
from app.core.exceptions import DocumentParsingError

log = structlog.get_logger(__name__)

# Fields to skip — internal MongoDB metadata, not useful for retrieval
_SKIP_FIELDS = {"_id", "__v"}

# Max depth to recurse into nested objects
_MAX_DEPTH = 3


class MongoDBLoader(BaseLoader):

    def supported_mime_types(self) -> list[str]:
        return []  # not a file loader

    def supported_structured_types(self) -> list[str]:
        return ["mongodb"]

    async def load(self, raw_doc: RawDocument) -> ParsedDocument:
        """
        Convert a MongoDB document dict into structured Markdown.
        No async work needed — pure string formatting.
        """
        if not raw_doc.raw_data:
            raise DocumentParsingError(
                f"MongoDBLoader received empty raw_data for '{raw_doc.file_name}'"
            )

        doc = raw_doc.raw_data
        collection = raw_doc.extra_metadata.get("collection", raw_doc.file_name)
        database = raw_doc.extra_metadata.get("database", "")
        doc_id = raw_doc.extra_metadata.get("document_id", raw_doc.source_id)

        # Build heading
        db_prefix = f"{database}." if database else ""
        lines = [
            f"## {db_prefix}{collection} — {doc_id}",
            "",
        ]

        # Separate top-level scalar fields from nested objects/arrays
        scalar_fields = {}
        nested_fields = {}

        for key, val in doc.items():
            if key in _SKIP_FIELDS:
                continue
            if isinstance(val, (dict, list)):
                nested_fields[key] = val
            else:
                scalar_fields[key] = val

        # ── Top-level scalar fields ────────────────────────────────────────────
        if scalar_fields:
            lines.append("### Fields")
            lines.append("")
            for key, val in scalar_fields.items():
                formatted = self._format_value(val)
                lines.append(f"- **{key}**: {formatted}")
            lines.append("")

        # ── Nested objects and arrays ──────────────────────────────────────────
        for key, val in nested_fields.items():
            lines.append(f"### {key}")
            lines.append("")
            lines.extend(self._format_nested(val, depth=1))
            lines.append("")

        markdown_text = "\n".join(lines)

        log.debug(
            "loader.mongodb.loaded",
            collection=collection,
            doc_id=doc_id,
            fields=len(doc),
        )

        return ParsedDocument(
            markdown_text=markdown_text,
            extracted_metadata={
                "loader": "mongodb",
                "collection": collection,
                "database": database,
                "field_count": len(doc),
            },
        )

    def _format_value(self, val) -> str:
        """Format a scalar value for Markdown display."""
        if val is None:
            return "_null_"
        if isinstance(val, bool):
            return str(val).lower()
        return str(val)

    def _format_nested(self, val, depth: int) -> list[str]:
        """Recursively format nested objects/arrays as indented Markdown."""
        lines = []
        indent = "  " * (depth - 1)

        if depth > _MAX_DEPTH:
            # Too deep — just serialize as JSON string
            lines.append(f"{indent}- {json.dumps(val, default=str)[:200]}")
            return lines

        if isinstance(val, dict):
            for k, v in val.items():
                if k in _SKIP_FIELDS:
                    continue
                if isinstance(v, (dict, list)):
                    lines.append(f"{indent}- **{k}**:")
                    lines.extend(self._format_nested(v, depth + 1))
                else:
                    lines.append(f"{indent}- **{k}**: {self._format_value(v)}")

        elif isinstance(val, list):
            for i, item in enumerate(val[:20]):  # cap at 20 items
                if isinstance(item, dict):
                    lines.append(f"{indent}- Item {i + 1}:")
                    lines.extend(self._format_nested(item, depth + 1))
                else:
                    lines.append(f"{indent}- {self._format_value(item)}")
            if len(val) > 20:
                lines.append(f"{indent}- _... {len(val) - 20} more items_")

        return lines
