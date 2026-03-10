"""
Base connector abstractions.

RawDocument   — the standardised output of every connector
DeltaItem     — wraps RawDocument with a change type (upsert/deleted)
BaseConnector — the contract every connector must implement

Why DeltaItem instead of just RawDocument?
  The connector needs to signal THREE things:
  1. "Here is a new/changed file"  → type="upsert",  raw_doc populated
  2. "This file was deleted"       → type="deleted", only source_id matters
  Without DeltaItem the caller can't distinguish these cases.

Two content types in RawDocument:

  content_type="file"
    Used by: SharePoint, local filesystem connectors
    content       : raw bytes (PDF, DOCX, PPTX etc.)
    mime_type     : used by LoaderFactory to pick the right loader
    raw_data      : None
    markdown_text : None (loader produces this)

  content_type="structured"
    Used by: SQL, MongoDB connectors
    content       : None
    mime_type     : None
    raw_data      : dict of one row/document from the source
                    SQLLoader / MongoDBLoader converts this to Markdown
    structured_source_type : "sql" | "mongodb" — LoaderFactory uses this
                              to select the correct structured loader

  In both cases the loader converts the raw input into Markdown text.
  The pipeline is identical for both content types — no bypasses.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, AsyncGenerator, Literal

from pydantic import BaseModel, Field


class RawDocument(BaseModel):
    """
    Standardised output of a connector.

    File sources (SharePoint, local):
      content_type           = "file"
      content                = raw bytes
      mime_type              = "application/pdf" | "application/vnd..." etc.
      raw_data               = None
      structured_source_type = None

    Structured sources (SQL, MongoDB):
      content_type           = "structured"
      content                = None
      mime_type              = None
      raw_data               = {"column": "value", ...}  ← one row or document
      structured_source_type = "sql" | "mongodb"

    Always required regardless of content_type:
      source_id, file_name, full_path, source_url, content_hash
    """

    # ── Content type discriminator ─────────────────────────────────────────────
    content_type: Literal["file", "structured"] = "file"

    # ── File content (content_type="file") ────────────────────────────────────
    content: bytes | None = None
    mime_type: str | None = None

    # ── Structured content (content_type="structured") ────────────────────────
    # Raw dict from SQL row or MongoDB document.
    # The loader (SQLLoader / MongoDBLoader) converts this to Markdown.
    raw_data: dict[str, Any] | None = None
    structured_source_type: Literal["sql", "mongodb"] | None = None

    # ── Identity — always required ─────────────────────────────────────────────
    source_id: str  # unique ID within this repo
    file_name: str  # display name e.g. "Q1_Report.pdf" or "customers"
    full_path: str  # e.g. "/finance/Q1.pdf" or "/tables/customers/C-001"
    source_url: str  # direct link to source item
    content_hash: str  # sha256 of content/raw_data — used for dedup

    # ── Temporal ───────────────────────────────────────────────────────────────
    last_modified_at_source: datetime | None = None

    # ── Source-specific extras ─────────────────────────────────────────────────
    # Connector-specific metadata preserved through the pipeline into the
    # ingested_documents registry and vector store metadata.
    # Examples:
    #   SharePoint : {"sharepoint_item_id": "...", "sharepoint_author": "...", "sharepoint_site": "..."}
    #   SQL        : {"table_name": "customers", "primary_key": "C-001", "schema": "dbo"}
    #   MongoDB    : {"collection": "orders", "document_id": "64abc...", "database": "crm"}
    extra_metadata: dict = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    def is_file(self) -> bool:
        """True if this document came from a file-based source."""
        return self.content_type == "file"

    def is_structured(self) -> bool:
        """True if this document came from a structured data source (SQL/MongoDB)."""
        return self.content_type == "structured"


class DeltaItem(BaseModel):
    """
    A single item yielded by a connector during a delta scan.

    type="upsert"  → new or changed item, raw_doc is populated
    type="deleted" → item no longer exists at source, only source_id matters
                     raw_doc will be None
    """

    type: Literal["upsert", "deleted"]
    source_id: str
    raw_doc: RawDocument | None = None  # None when type="deleted"

    model_config = {"arbitrary_types_allowed": True}


class BaseConnector(ABC):
    """
    Abstract base class for all data source connectors.

    File connectors       (SharePoint, local)  → yield content_type="file"
    Structured connectors (SQL, MongoDB)       → yield content_type="structured"

    The LoaderFactory selects the correct loader for each content type.
    The ingestion pipeline is identical for both — no special cases.

    Subclasses must implement:
      connect()          — verify connectivity, raise ConnectorError if unreachable
      fetch_documents()  — async generator yielding DeltaItems
      disconnect()       — clean up connections

    Subclasses may override:
      delta_token              — return sync token after fetch (SharePoint)
      supports_native_deletes  — return True if connector yields deleted DeltaItems
    """

    @abstractmethod
    async def connect(self) -> None:
        """
        Verify connectivity to the data source.
        Raise ConnectorError if the source is unreachable or credentials are invalid.
        Called once before fetch_documents().
        """
        pass

    @abstractmethod
    async def fetch_documents(self) -> AsyncGenerator[DeltaItem, None]:
        """
        Yield DeltaItems for all new, changed, and deleted items.

        Must be an async generator — yields one item at a time so the
        ingestion pipeline can process documents without loading everything
        into memory first.

        For upserts:  yield DeltaItem(type="upsert",  source_id=..., raw_doc=...)
        For deletes:  yield DeltaItem(type="deleted", source_id=..., raw_doc=None)
                      (only if supports_native_deletes=True)
        """
        yield  # type: ignore[misc]

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Clean up connections, close HTTP sessions, release resources.
        Called once after fetch_documents() completes.
        """
        pass

    @property
    def delta_token(self) -> str | None:
        """
        Sync token produced by this run. Saved to MongoDB after a successful
        ingestion so the next run can fetch only changes since this token.

        Override in connectors that support delta/incremental APIs.
        Example: SharePoint Graph API delta queries return a deltaLink token.

        Returns None by default (no delta token support).
        """
        return None

    @property
    def supports_native_deletes(self) -> bool:
        """
        Whether this connector explicitly yields DeltaItem(type="deleted")
        for items that have been removed from the source.

        True  → connector reports deletes natively (e.g. SharePoint delta API)
                 Worker handles them in Step 4 as they arrive in the stream.

        False → connector does NOT report deletes (e.g. local, SQL, MongoDB)
                 Worker detects implicit deletes in Step 5 by comparing
                 seen_source_ids against the ingested_documents registry.

        Returns False by default.
        """
        return False
