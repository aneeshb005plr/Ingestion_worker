"""
StructuredFormatter — converts structured data (SQL rows, MongoDB documents)
into consistent, retrieval-optimised Markdown.

Why this exists:
  SQL and MongoDB connectors don't need a loader — their data is already
  structured. But we need consistent, high-quality Markdown formatting
  so the chunker and retrieval system work well on structured data too.

  Without this, each connector would format Markdown differently.
  With this, all structured sources produce the same format.

Usage in a SQL connector:
    formatter = StructuredFormatter()
    for row in rows:
        markdown = formatter.from_sql_row(
            table_name="customers",
            row=row,
            primary_key="customer_id",
        )
        yield DeltaItem(type="upsert", source_id=..., raw_doc=RawDocument(
            content_type="structured",
            markdown_text=markdown,
            ...
        ))

Usage in a MongoDB connector:
    formatter = StructuredFormatter()
    for doc in collection.find():
        markdown = formatter.from_mongo_document(
            collection_name="orders",
            document=doc,
        )
        yield DeltaItem(...)
"""

import json
from datetime import datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Fields that are typically noisy/useless for retrieval — skip by default
_DEFAULT_SKIP_FIELDS = frozenset(
    {
        "_id",
        "__v",
        "created_at",
        "updated_at",
        "deleted_at",
        "password",
        "password_hash",
        "refresh_token",
        "access_token",
    }
)


class StructuredFormatter:
    """
    Converts structured records into retrieval-optimised Markdown.

    The output format is designed to work well with our two-stage chunker:
      - Top-level heading (#) = table/collection name
      - Sub-heading (##) = record identity (primary key or _id)
      - Fields as bold key: value pairs
      - Nested objects as sub-sections
      - Arrays as bullet lists
    """

    def __init__(self, skip_fields: set[str] | None = None) -> None:
        self._skip_fields = skip_fields or _DEFAULT_SKIP_FIELDS

    def from_sql_row(
        self,
        table_name: str,
        row: dict[str, Any],
        primary_key: str | None = None,
        column_descriptions: dict[str, str] | None = None,
    ) -> str:
        """
        Format a single SQL row as Markdown.

        Args:
            table_name:          e.g. "customers", "orders"
            row:                 dict of column_name → value
            primary_key:         column name of the primary key (for the heading)
            column_descriptions: optional human-readable descriptions per column
                                 e.g. {"cust_id": "Customer identifier", "amt": "Order amount in USD"}
                                 Makes retrieval much better — users ask in natural language

        Example output:
            # Table: customers
            ## Record: C-001

            **customer_id**: C-001
            **name**: Acme Corporation
            *Customer full legal name*
            **region**: APAC
            **annual_revenue**: 5200000
            *Total annual revenue in USD*
        """
        lines = [f"# Table: {table_name}"]

        # Use primary key value as record identifier if available
        pk_value = row.get(primary_key, "unknown") if primary_key else "record"
        lines.append(f"## Record: {pk_value}")
        lines.append("")

        for col, value in row.items():
            if col in self._skip_fields:
                continue
            formatted_value = self._format_value(value)
            lines.append(f"**{col}**: {formatted_value}")
            # Add human-readable description if provided
            if column_descriptions and col in column_descriptions:
                lines.append(f"*{column_descriptions[col]}*")

        return "\n".join(lines)

    def from_mongo_document(
        self,
        collection_name: str,
        document: dict[str, Any],
        id_field: str = "_id",
        field_descriptions: dict[str, str] | None = None,
        skip_fields: set[str] | None = None,
    ) -> str:
        """
        Format a single MongoDB document as Markdown.

        Args:
            collection_name:    e.g. "orders", "products"
            document:           the MongoDB document dict
            id_field:           field to use as record identifier (default: "_id")
            field_descriptions: optional human-readable descriptions per field
            skip_fields:        fields to exclude (merged with defaults)

        Example output:
            # Collection: orders

            ## Document: 64abc123def

            **order_id**: ORD-2026-001
            **customer**: Acme Corp
            **status**: shipped
            **items**:
              - product: Widget A, qty: 3, price: 29.99
              - product: Widget B, qty: 1, price: 49.99
            **total**: 139.96
            **shipping_address**: 123 Main St, Singapore
        """
        effective_skip = (skip_fields or set()) | self._skip_fields
        doc_id = document.get(id_field, "unknown")
        # Convert ObjectId to string if needed
        doc_id_str = str(doc_id)

        lines = [f"# Collection: {collection_name}"]
        lines.append(f"## Document: {doc_id_str}")
        lines.append("")

        for field, value in document.items():
            if field in effective_skip:
                continue
            if isinstance(value, list):
                lines.append(f"**{field}**:")
                for item in value:
                    lines.append(f"  - {self._format_value(item)}")
            elif isinstance(value, dict):
                lines.append(f"**{field}**:")
                for k, v in value.items():
                    lines.append(f"  - **{k}**: {self._format_value(v)}")
            else:
                lines.append(f"**{field}**: {self._format_value(value)}")

            if field_descriptions and field in field_descriptions:
                lines.append(f"*{field_descriptions[field]}*")

        return "\n".join(lines)

    def from_records_batch(
        self,
        source_name: str,
        records: list[dict[str, Any]],
        record_id_field: str,
        source_type: str = "table",
    ) -> str:
        """
        Format multiple records into a single Markdown document.
        Use this when you want a whole table/collection as one chunk context
        rather than one document per row.

        Useful for small reference tables (e.g. country codes, status mappings)
        where individual rows have no meaning without the others.
        """
        heading = "Table" if source_type == "table" else "Collection"
        lines = [f"# {heading}: {source_name}", ""]

        for record in records:
            record_id = record.get(record_id_field, "unknown")
            lines.append(f"## {record_id}")
            for field, value in record.items():
                if field in self._skip_fields or field == record_id_field:
                    continue
                lines.append(f"- **{field}**: {self._format_value(value)}")
            lines.append("")

        return "\n".join(lines)

    def _format_value(self, value: Any) -> str:
        """Format a single value as a readable string."""
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M UTC")
        if isinstance(value, (dict, list)):
            return json.dumps(value, default=str, ensure_ascii=False)
        return str(value)
