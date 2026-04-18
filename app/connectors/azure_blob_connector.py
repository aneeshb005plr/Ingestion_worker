"""
AzureBlobConnector — Production Grade
Authentication: Connection string OR DefaultAzureCredential (Managed Identity / Entra ID)
Required RBAC role: Storage Blob Data Reader (read) or Storage Blob Data Contributor (read+write)

Azure Blob Storage has NO native delta API (unlike SharePoint/OneDrive Graph delta).
Delta detection strategy:
  Full scan  (last_run_at=None) — list all blobs, ingest all matching files
  Delta scan (last_run_at=datetime) — list blobs, filter by last_modified > last_run_at
  Deletion detection — compare current blob list against ingested_documents registry
    (supports_native_deletes=False → worker Step 5 handles implicit deletes)

Two modes:
  Full scan  (last_run_at=None) — fetches all blobs in container/prefix
  Delta scan (last_run_at=str)  — fetches only blobs modified since last run

DeltaItem types yielded:
  type="upsert" — new or changed blob (raw_doc populated with content bytes)
  (deletions handled by worker Step 5 registry diff — not native)

connector_config keys:
  container_name           — Azure Blob container name
  prefix                   — Blob name prefix to scope the scan (optional, e.g. "documents/XLOS/")
  file_extensions_include  — e.g. [".pdf", ".docx"] — empty = allow all supported types
  file_extensions_exclude  — e.g. [".tmp"] — always skip
  max_file_size_mb         — Skip blobs larger than this (default 50)

credentials keys (one of):
  connection_string        — Full Azure Storage connection string (for dev/test)
  OR
  account_url              — "https://{account}.blob.core.windows.net" (for prod with Managed Identity)
  account_name + account_key — Storage account key auth (for dev)

Delta detection note:
  Azure Blob does not have a delta API. We use last_modified timestamp comparison.
  last_run_at is stored in repo_config.state.last_successful_run_at by the worker.
  On delta scan: yield blobs where blob.last_modified > last_run_at.
  Deletions detected by worker Step 5 (registry diff).

Package version:
  azure-storage-blob>=12.24.0  — latest stable as of 2026, supports service version 2025-11-05
  azure-identity>=1.19.0       — DefaultAzureCredential for Managed Identity / Entra ID
"""

import asyncio
import hashlib
import io
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import structlog
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient, ContainerClient

from app.connectors.base import BaseConnector, DeltaItem, RawDocument

log = structlog.get_logger(__name__)

# Hidden/temp file prefixes — always skip
HIDDEN_PREFIXES = ("~$", ".~", "._", "$")

# Supported ingestion file extensions
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".txt",
    ".md",
    ".csv",
    ".html",
    ".htm",
    ".xml",
    ".json",
}


class AzureBlobConnector(BaseConnector):
    """
    Connects to Azure Blob Storage and ingests files from a container.

    Delta detection via last_modified timestamp:
      - Full scan: ingest all matching blobs
      - Delta scan: ingest only blobs modified after last_run_at
      - Deletion detection: worker Step 5 registry diff (not native)

    Authentication priority:
      1. connection_string (full connection string)
      2. account_url + DefaultAzureCredential (Managed Identity / Entra ID)
      3. account_url + account_key (shared key auth)
    """

    # Azure Blob has no native delete API — worker Step 5 handles deletions
    supports_native_deletes = False

    def __init__(
        self,
        config: dict,
        credentials: dict,
        last_run_at: Optional[datetime] = None,
    ) -> None:
        self._container_name: str = config["container_name"]
        self._prefix: str = config.get("prefix", "").lstrip("/")
        self._extensions_include = [
            e.lower() for e in config.get("file_extensions_include", [])
        ]
        self._extensions_exclude = [
            e.lower() for e in config.get("file_extensions_exclude", [])
        ]
        self._max_bytes = config.get("max_file_size_mb", 50) * 1024 * 1024

        self._credentials = credentials
        self._last_run_at = last_run_at  # datetime or None

        # Azure SDK clients — created in connect()
        self._blob_service_client: Optional[BlobServiceClient] = None
        self._container_client: Optional[ContainerClient] = None
        self._credential: Optional[DefaultAzureCredential] = None

    # ── BaseConnector interface ───────────────────────────────────────────────

    async def connect(self) -> None:
        """Create Azure Blob client and verify container exists."""
        self._blob_service_client = self._build_service_client()
        self._container_client = self._blob_service_client.get_container_client(
            self._container_name
        )
        try:
            # Verify container is accessible
            await self._container_client.get_container_properties()
            log.info(
                "azureblob.connected",
                container=self._container_name,
                prefix=self._prefix or "(none)",
                delta_mode=bool(self._last_run_at),
                last_run_at=(
                    self._last_run_at.isoformat() if self._last_run_at else None
                ),
            )
        except ResourceNotFoundError:
            raise ValueError(
                f"Azure Blob container '{self._container_name}' not found. "
                "Check container_name and storage account credentials."
            )
        except AzureError as e:
            raise ValueError(
                f"Azure Blob connection failed for container '{self._container_name}': {e}"
            )

    async def fetch_documents(self) -> AsyncIterator[DeltaItem]:
        """
        Yield DeltaItems for all new or changed blobs.

        Full scan (last_run_at=None):
          - Lists all blobs under prefix
          - Yields all matching files

        Delta scan (last_run_at=datetime):
          - Lists all blobs under prefix
          - Yields only blobs where last_modified > last_run_at

        Deletion detection:
          - NOT native — worker Step 5 compares seen_source_ids
            against ingested_documents registry to detect deleted blobs
        """
        is_delta = self._last_run_at is not None
        scan_mode = "delta" if is_delta else "full"
        log.info(
            "azureblob.scan_start",
            container=self._container_name,
            prefix=self._prefix or "(none)",
            mode=scan_mode,
            since=self._last_run_at.isoformat() if self._last_run_at else None,
        )

        processed = 0
        skipped = 0

        async for blob in self._container_client.list_blobs(
            name_starts_with=self._prefix or None,
            include=["metadata"],  # include metadata for extra context
        ):
            name = blob.name

            # Extract just the filename from the blob path
            file_name = name.rsplit("/", 1)[-1] if "/" in name else name

            # ── Skip hidden/temp files ───────────────────────────────────────
            if any(file_name.startswith(p) for p in HIDDEN_PREFIXES):
                skipped += 1
                continue

            # ── Extension filtering ──────────────────────────────────────────
            if not self._should_process(file_name):
                skipped += 1
                continue

            # ── File size check ──────────────────────────────────────────────
            blob_size = blob.size or 0
            if blob_size > self._max_bytes:
                log.warning(
                    "azureblob.skip_oversized",
                    blob=name,
                    size_mb=round(blob_size / 1024 / 1024, 1),
                    limit_mb=self._max_bytes // (1024 * 1024),
                )
                skipped += 1
                continue

            # ── Delta filter — skip blobs not modified since last run ────────
            if is_delta and self._last_run_at:
                blob_modified = blob.last_modified
                if blob_modified:
                    # Ensure timezone-aware comparison
                    last_run = self._last_run_at
                    if last_run.tzinfo is None:
                        last_run = last_run.replace(tzinfo=timezone.utc)
                    if blob_modified.tzinfo is None:
                        blob_modified = blob_modified.replace(tzinfo=timezone.utc)
                    if blob_modified <= last_run:
                        skipped += 1
                        continue

            # ── Build RawDocument ────────────────────────────────────────────
            raw_doc = await self._build_raw_document(blob, file_name)
            if raw_doc is None:
                skipped += 1
                continue

            processed += 1
            source_id = self._make_source_id(name)
            yield DeltaItem(type="upsert", source_id=source_id, raw_doc=raw_doc)

        log.info(
            "azureblob.scan_complete",
            container=self._container_name,
            mode=scan_mode,
            processed=processed,
            skipped=skipped,
        )

    async def disconnect(self) -> None:
        """Close Azure SDK clients and credential."""
        if self._container_client:
            await self._container_client.close()
            self._container_client = None
        if self._blob_service_client:
            await self._blob_service_client.close()
            self._blob_service_client = None
        if self._credential:
            await self._credential.close()
            self._credential = None

    # ── No delta_token for Azure Blob ─────────────────────────────────────────
    # Delta detection uses last_run_at timestamp (stored by worker).
    # worker.py reads repo_state["last_successful_run_at"] and passes it here.

    # ── Document building ─────────────────────────────────────────────────────

    async def _build_raw_document(
        self,
        blob,
        file_name: str,
    ) -> Optional[RawDocument]:
        """Download blob content and build RawDocument."""
        blob_name = blob.name

        try:
            # Download blob content
            blob_client = self._container_client.get_blob_client(blob_name)
            downloader = await blob_client.download_blob()
            content = await downloader.readall()
        except ResourceNotFoundError:
            log.warning("azureblob.blob_not_found", blob=blob_name)
            return None
        except AzureError as e:
            log.error("azureblob.download_failed", blob=blob_name, error=str(e))
            return None

        if not content:
            log.warning("azureblob.empty_blob", blob=blob_name)
            return None

        # Content hash — sha256 of content bytes
        content_hash = hashlib.sha256(content).hexdigest()

        # Full path — blob name relative to prefix
        if self._prefix and blob_name.startswith(self._prefix):
            full_path = blob_name[len(self._prefix) :].lstrip("/")
        else:
            full_path = blob_name

        # Source URL — direct blob URL
        blob_client = self._container_client.get_blob_client(blob_name)
        source_url = blob_client.url

        # Last modified
        last_modified = blob.last_modified
        if last_modified and last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=timezone.utc)

        # MIME type
        mime_type = self._get_mime_type(file_name)

        # Blob metadata (user-defined tags if present)
        blob_metadata = blob.metadata or {}

        return RawDocument(
            content_type="file",
            content=content,
            mime_type=mime_type,
            source_id=self._make_source_id(blob_name),
            file_name=file_name,
            full_path=full_path,
            source_url=source_url,
            content_hash=content_hash,
            last_modified_at_source=last_modified,
            extra_metadata={
                "azureblob_blob_name": blob_name,
                "azureblob_container": self._container_name,
                "azureblob_size": blob.size,
                "azureblob_etag": blob.etag or "",
                "azureblob_content_type": (
                    blob.content_settings.content_type
                    if blob.content_settings
                    else None
                ),
                # User-defined metadata tags from the blob
                **{f"azureblob_meta_{k}": v for k, v in blob_metadata.items()},
            },
        )

    # ── Client construction ───────────────────────────────────────────────────

    def _build_service_client(self) -> BlobServiceClient:
        """
        Build BlobServiceClient from credentials.

        Priority:
          1. connection_string → direct connection (dev/test)
          2. account_url + account_key → shared key auth
          3. account_url → DefaultAzureCredential (Managed Identity / Entra ID)
        """
        conn_str = self._credentials.get("connection_string")
        account_url = self._credentials.get("account_url")
        account_key = self._credentials.get("account_key")
        account_name = self._credentials.get("account_name")

        if conn_str:
            log.debug("azureblob.auth", method="connection_string")
            return BlobServiceClient.from_connection_string(conn_str)

        if account_url and account_key:
            log.debug("azureblob.auth", method="shared_key")
            from azure.storage.blob.aio import BlobServiceClient as _BSC

            return _BSC(account_url=account_url, credential=account_key)

        if account_name and account_key:
            url = f"https://{account_name}.blob.core.windows.net"
            log.debug("azureblob.auth", method="shared_key_from_name")
            return BlobServiceClient(account_url=url, credential=account_key)

        if account_url:
            # DefaultAzureCredential — Managed Identity / Entra ID / local az login
            log.debug("azureblob.auth", method="default_azure_credential")
            self._credential = DefaultAzureCredential()
            return BlobServiceClient(
                account_url=account_url,
                credential=self._credential,
            )

        raise ValueError(
            "AzureBlobConnector requires one of: "
            "connection_string, account_url, or account_name+account_key in credentials."
        )

    # ── File filtering ────────────────────────────────────────────────────────

    def _should_process(self, file_name: str) -> bool:
        """Return True if this blob should be ingested."""
        ext = self._get_extension(file_name)

        if ext in self._extensions_exclude:
            return False

        if self._extensions_include:
            return ext in self._extensions_include

        return ext in SUPPORTED_EXTENSIONS

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_source_id(self, blob_name: str) -> str:
        """Stable source ID scoped to this container."""
        return f"azureblob:{self._container_name}:{blob_name}"

    @staticmethod
    def _get_extension(file_name: str) -> str:
        """Return lowercase file extension including dot."""
        if "." in file_name:
            return "." + file_name.rsplit(".", 1)[-1].lower()
        return ""

    @staticmethod
    def _get_mime_type(file_name: str) -> str:
        """Map file extension to MIME type."""
        ext = AzureBlobConnector._get_extension(file_name)
        mime_map = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".ppt": "application/vnd.ms-powerpoint",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".csv": "text/csv",
            ".html": "text/html",
            ".htm": "text/html",
            ".xml": "application/xml",
            ".json": "application/json",
        }
        return mime_map.get(ext, "application/octet-stream")
