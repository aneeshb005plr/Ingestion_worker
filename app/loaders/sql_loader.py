"""
SQLLoader — converts a SQL row (dict) into structured Markdown.

Input:  RawDocument with raw_data = {"col1": "val1", "col2": "val2", ...}
Output: ParsedDocument with clean Markdown

Markdown format produced:
  ## {table_name} — Record {primary_key}
  | Column | Value |
  |--------|-------|
  | col1   | val1  |
  | col2   | val2  |

Why a table format?
  - Preserves column names as context alongside values
  - The chunker keeps the table together as one unit
  - Retrieval: "what is the status of order O-123?" matches the row cleanly

The SQL connector puts table_name and primary_key in extra_metadata.
"""

import structlog

from app.loaders.base import BaseLoader, ParsedDocument
from app.connectors.base import RawDocument
from app.core.exceptions import DocumentParsingError

log = structlog.get_logger(__name__)


class SQLLoader(BaseLoader):

    def supported_mime_types(self) -> list[str]:
        return []  # not a file loader

    def supported_structured_types(self) -> list[str]:
        return ["sql"]

    async def load(self, raw_doc: RawDocument) -> ParsedDocument:
        """
        Convert a SQL row dict into a Markdown table.
        No async work needed — pure string formatting.
        """
        if not raw_doc.raw_data:
            raise DocumentParsingError(
                f"SQLLoader received empty raw_data for '{raw_doc.file_name}'"
            )

        row = raw_doc.raw_data
        table_name = raw_doc.extra_metadata.get("table_name", raw_doc.file_name)
        primary_key = raw_doc.extra_metadata.get("primary_key", raw_doc.source_id)
        schema = raw_doc.extra_metadata.get("schema", "")

        # Build heading
        schema_prefix = f"{schema}." if schema else ""
        heading = f"## {schema_prefix}{table_name} — {primary_key}"

        # Build Markdown table — one row per column
        # Using a key-value table rather than a wide columnar table
        # because column names are critical context for retrieval
        lines = [
            heading,
            "",
            "| Column | Value |",
            "|--------|-------|",
        ]

        for col, val in row.items():
            # Handle None, long text, nested dicts
            if val is None:
                formatted_val = "_null_"
            elif isinstance(val, dict):
                # Nested object — format as JSON-like string
                formatted_val = str(val)
            elif isinstance(val, list):
                formatted_val = ", ".join(str(v) for v in val)
            else:
                # Escape pipe characters in values to avoid breaking Markdown table
                formatted_val = str(val).replace("|", "\\|")

            lines.append(f"| {col} | {formatted_val} |")

        markdown_text = "\n".join(lines)

        log.debug(
            "loader.sql.loaded",
            table=table_name,
            primary_key=primary_key,
            columns=len(row),
        )

        return ParsedDocument(
            markdown_text=markdown_text,
            extracted_metadata={
                "loader": "sql",
                "table_name": table_name,
                "schema": schema,
                "column_count": len(row),
            },
        )
