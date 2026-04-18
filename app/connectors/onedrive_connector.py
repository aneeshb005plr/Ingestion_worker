"""
OneDriveConnector — Production Grade
Authentication: OAuth2 client credentials (application permissions)
Required Azure App Registration permissions: Files.Read.All (Application)

Shares 95% of logic with SharePointConnector — both use Microsoft Graph API
driveItem delta endpoint. The only difference is drive discovery:
  SharePoint: drive from site_id + drive_id
  OneDrive:   drive from user_id (user's personal OneDrive for Business)

Two modes:
  Full scan  (delta_token=None) — fetches all files, saves deltaLink
  Delta scan (delta_token=str)  — fetches only changed/deleted since last run

DeltaItem types yielded:
  type="upsert"  → new or changed file (raw_doc populated with content bytes)
  type="deleted" → file removed (source_id only, raw_doc=None)

Delta deletion detection:
  Graph API delta endpoint returns items with "deleted" facet for removed files.
  We detect: item.get("deleted") is not None → yield DeltaItem(type="deleted")
  This is native delete detection — no registry diff needed.

connector_config keys:
  user_id                  — Azure AD user UPN or object ID (e.g. "aneesh@pwc.com")
  drive_id                 — Specific drive ID (None = user's default OneDrive)
  root_folder              — Subfolder path to scope the scan (optional, e.g. "Documents/XLOS")
  file_extensions_include  — e.g. [".pdf", ".docx"] — empty = allow all
  file_extensions_exclude  — e.g. [".tmp"] — always skip
  max_file_size_mb         — Skip files larger than this (default 50)

credentials keys:
  tenant_id      — Azure AD tenant ID
  client_id      — App registration client ID
  client_secret  — App registration client secret

Graph API endpoints used:
  Drive discovery:  GET /users/{user_id}/drive
                    GET /users/{user_id}/drives (list all drives)
  Delta:            GET /drives/{drive_id}/root/delta
  Download:         GET /drives/{drive_id}/items/{item_id}/content
"""

import asyncio
import hashlib
import time
from datetime import datetime
from typing import AsyncIterator, Optional

import aiohttp
import structlog

from app.connectors.base import BaseConnector, DeltaItem, RawDocument

log = structlog.get_logger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

# Office temp / hidden file prefixes — always skip
HIDDEN_PREFIXES = ("~$", ".~", "._")

# Supported ingestion file extensions (same as SharePoint)
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


class OneDriveConnector(BaseConnector):
    """
    Connects to a user's OneDrive for Business via Microsoft Graph API.

    Reuses the same Graph API delta pattern as SharePointConnector.
    Drive is discovered from user_id instead of site_id.
    """

    supports_native_deletes = True

    def __init__(
        self,
        config: dict,
        credentials: dict,
        delta_token: Optional[str] = None,
    ) -> None:
        self._user_id: str = config["user_id"]
        self._drive_id_override: Optional[str] = config.get("drive_id")
        self._root_folder: str = config.get("root_folder", "").strip("/")
        self._extensions_include = [
            e.lower() for e in config.get("file_extensions_include", [])
        ]
        self._extensions_exclude = [
            e.lower() for e in config.get("file_extensions_exclude", [])
        ]
        self._max_bytes = config.get("max_file_size_mb", 50) * 1024 * 1024

        self._az_tenant_id: str = credentials["tenant_id"]
        self._client_id: str = credentials["client_id"]
        self._client_secret: str = credentials["client_secret"]

        self._saved_delta_token: Optional[str] = delta_token
        self._new_delta_token: Optional[str] = None

        # Resolved at connect() time
        self._resolved_drive_id: Optional[str] = None

        # Token cache
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0

        # HTTP session — created in connect(), closed in disconnect()
        self._session: Optional[aiohttp.ClientSession] = None

    # ── BaseConnector interface ───────────────────────────────────────────────

    async def connect(self) -> None:
        """Verify connectivity and resolve the OneDrive drive ID."""
        self._session = aiohttp.ClientSession()
        try:
            await self._get_token()
            self._resolved_drive_id = await self._resolve_drive_id()
            log.info(
                "onedrive.connected",
                user_id=self._user_id,
                drive_id=self._resolved_drive_id,
            )
        except Exception as e:
            await self._session.close()
            self._session = None
            raise

    async def fetch_documents(self) -> AsyncIterator[DeltaItem]:
        """
        Yield DeltaItems for all new, changed, and deleted files.

        Full scan (delta_token=None):
          - Fetches all files via delta endpoint (initial enumeration)
          - Saves deltaLink for next run

        Delta scan (delta_token=str):
          - Fetches only changes since last run via saved deltaLink
          - Yields deleted items natively from "deleted" facet
        """
        drive_id = self._resolved_drive_id
        seen_source_ids: set[str] = set()

        # Build delta URL
        if self._saved_delta_token:
            # Delta scan — use saved deltaLink directly
            delta_url = self._saved_delta_token
            log.info("onedrive.delta_scan", drive_id=drive_id)
        else:
            # Full scan — start fresh delta enumeration
            delta_url = f"{GRAPH_BASE}/drives/{drive_id}/root/delta"
            log.info("onedrive.full_scan", drive_id=drive_id)

        # Paginate through all delta results
        while delta_url:
            data = await self._graph_get(delta_url)
            items = data.get("value", [])

            for item in items:
                item_id = item.get("id", "")
                name = item.get("name", "")

                # Skip root item
                if name == "root" or not item_id:
                    continue

                # ── Deletion detection ───────────────────────────────────────
                # Graph API delta returns "deleted" facet for removed items
                if item.get("deleted") is not None:
                    source_id = self._make_source_id(item_id)
                    log.debug(
                        "onedrive.item_deleted",
                        item_id=item_id,
                        name=name,
                    )
                    yield DeltaItem(type="deleted", source_id=source_id)
                    continue

                # ── Skip folders — only process files ────────────────────────
                if "folder" in item:
                    continue

                # ── Root folder filter ───────────────────────────────────────
                if self._root_folder:
                    parent_path = self._get_item_path(item)
                    if not self._is_under_root_folder(parent_path):
                        continue

                # ── File filter checks ───────────────────────────────────────
                if not self._should_process(name, item):
                    continue

                source_id = self._make_source_id(item_id)
                seen_source_ids.add(source_id)

                # ── Build RawDocument ────────────────────────────────────────
                raw_doc = await self._build_raw_document(item, drive_id)
                if raw_doc is None:
                    continue

                yield DeltaItem(type="upsert", source_id=source_id, raw_doc=raw_doc)

            # ── Pagination ───────────────────────────────────────────────────
            next_link = data.get("@odata.nextLink")
            delta_link = data.get("@odata.deltaLink")

            if delta_link:
                # Save new delta token for next run
                self._new_delta_token = delta_link
                delta_url = None  # Done
            elif next_link:
                delta_url = next_link
            else:
                delta_url = None

        log.info(
            "onedrive.fetch_complete",
            drive_id=drive_id,
            has_delta_token=bool(self._new_delta_token),
        )

    async def disconnect(self) -> None:
        """Close HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def delta_token(self) -> Optional[str]:
        """Return the new deltaLink produced by this run."""
        return self._new_delta_token

    # ── Drive resolution ──────────────────────────────────────────────────────

    async def _resolve_drive_id(self) -> str:
        """
        Resolve the OneDrive drive ID for this user.

        Priority:
          1. drive_id_override from config (explicit)
          2. User's default OneDrive drive (GET /users/{user_id}/drive)
        """
        if self._drive_id_override:
            return self._drive_id_override

        # Get user's default OneDrive drive
        url = f"{GRAPH_BASE}/users/{self._user_id}/drive"
        data = await self._graph_get(url)
        drive_id = data.get("id")
        if not drive_id:
            raise ValueError(
                f"Could not resolve OneDrive drive for user '{self._user_id}'. "
                "Ensure the user has OneDrive provisioned and the app has Files.Read.All permission."
            )
        return drive_id

    # ── Document building ─────────────────────────────────────────────────────

    async def _build_raw_document(
        self,
        item: dict,
        drive_id: str,
    ) -> Optional[RawDocument]:
        """Build a RawDocument from a Graph API driveItem."""
        item_id = item["id"]
        name = item.get("name", "unknown")

        # File size check
        size = item.get("size", 0)
        if size > self._max_bytes:
            log.warning(
                "onedrive.skip_oversized",
                name=name,
                size_mb=round(size / 1024 / 1024, 1),
                limit_mb=self._max_bytes // (1024 * 1024),
            )
            return None

        # Download file content
        content = await self._download_item(item_id, drive_id)
        if content is None:
            return None

        # Build full_path from parentReference
        parent_path = self._get_item_path(item)
        full_path = f"{parent_path}/{name}" if parent_path else name

        # Strip root_folder prefix from full_path for clean metadata
        if self._root_folder:
            prefix = f"/{self._root_folder}/"
            if full_path.startswith(prefix):
                full_path = full_path[len(prefix) :]
            elif full_path.startswith(self._root_folder + "/"):
                full_path = full_path[len(self._root_folder) + 1 :]

        # Source URL — web link to the file
        source_url = item.get("webUrl") or f"https://onedrive.live.com/?id={item_id}"

        # Last modified
        last_modified = None
        lm_str = item.get("lastModifiedDateTime")
        if lm_str:
            try:
                last_modified = datetime.fromisoformat(lm_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        # Content hash — use Graph API eTag or sha256 of content
        etag = item.get("eTag", "")
        content_hash = hashlib.sha256(
            (etag + str(size)).encode() if etag else content
        ).hexdigest()

        # MIME type from file info
        mime_type = self._get_mime_type(name)

        return RawDocument(
            content_type="file",
            content=content,
            mime_type=mime_type,
            source_id=self._make_source_id(item_id),
            file_name=name,
            full_path=full_path,
            source_url=source_url,
            content_hash=content_hash,
            last_modified_at_source=last_modified,
            extra_metadata={
                "onedrive_item_id": item_id,
                "onedrive_drive_id": drive_id,
                "onedrive_user_id": self._user_id,
                "onedrive_etag": etag,
                "onedrive_size": size,
            },
        )

    # ── File filtering ────────────────────────────────────────────────────────

    def _should_process(self, name: str, item: dict) -> bool:
        """Return True if this file should be ingested."""
        # Skip hidden/temp Office files
        if any(name.startswith(p) for p in HIDDEN_PREFIXES):
            return False

        ext = self._get_extension(name)

        # Extension exclude list (always wins)
        if ext in self._extensions_exclude:
            return False

        # Extension include list (if specified, must match)
        if self._extensions_include:
            return ext in self._extensions_include

        # Default: only supported extensions
        return ext in SUPPORTED_EXTENSIONS

    def _is_under_root_folder(self, item_path: str) -> bool:
        """Check if item path is under the configured root_folder."""
        if not self._root_folder:
            return True
        # item_path from Graph: "/drives/{id}/root:/Folder/SubFolder"
        # After split("root:")[-1]: "/Folder/SubFolder"
        # root_folder: "Folder/SubFolder"
        normalized = item_path.strip("/")
        root = self._root_folder.strip("/")
        return normalized == root or normalized.startswith(root + "/")

    def _get_item_path(self, item: dict) -> str:
        """
        Extract clean path from parentReference.path.
        Graph returns: "/drives/{drive-id}/root:/Folder/SubFolder"
        We return: "Folder/SubFolder"
        """
        parent_ref = item.get("parentReference", {})
        raw_path = parent_ref.get("path", "")
        # Strip the drive prefix: everything after "root:"
        if "root:" in raw_path:
            path = raw_path.split("root:")[-1].strip("/")
        else:
            path = raw_path.strip("/")
        return path

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_source_id(self, item_id: str) -> str:
        """Stable source ID scoped to this drive."""
        return f"onedrive:{self._resolved_drive_id}:{item_id}"

    @staticmethod
    def _get_extension(name: str) -> str:
        """Return lowercase file extension including dot."""
        if "." in name:
            return "." + name.rsplit(".", 1)[-1].lower()
        return ""

    @staticmethod
    def _get_mime_type(name: str) -> str:
        """Map file extension to MIME type."""
        ext = OneDriveConnector._get_extension(name)
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

    # ── Graph API auth + HTTP ─────────────────────────────────────────────────

    async def _get_token(self) -> str:
        """Get OAuth2 access token with 5-minute refresh buffer."""
        if self._access_token and time.time() < self._token_expiry - 300:
            return self._access_token

        url = TOKEN_URL.format(tenant_id=self._az_tenant_id)
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": GRAPH_SCOPE,
        }
        async with self._session.post(url, data=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()

        self._access_token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 3600)
        return self._access_token

    async def _graph_get(self, url: str) -> dict:
        """Execute a GET request against Microsoft Graph with retry on 429."""
        max_retries = 3
        for attempt in range(max_retries):
            token = await self._get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            async with self._session.get(url, headers=headers) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    log.warning(
                        "onedrive.throttled",
                        retry_after=retry_after,
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status == 410:
                    # Delta token expired — caller should do full scan
                    raise ValueError(
                        "OneDrive delta token expired (HTTP 410). "
                        "Trigger a full scan by resetting the delta_token."
                    )
                resp.raise_for_status()
                return await resp.json()

        raise RuntimeError(
            f"OneDrive Graph API GET failed after {max_retries} retries: {url}"
        )

    async def _download_item(self, item_id: str, drive_id: str) -> Optional[bytes]:
        """Download file content from OneDrive."""
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
        max_retries = 3
        for attempt in range(max_retries):
            token = await self._get_token()
            headers = {"Authorization": f"Bearer {token}"}
            async with self._session.get(
                url,
                headers=headers,
                allow_redirects=True,
            ) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status == 404:
                    log.warning("onedrive.item_not_found", item_id=item_id)
                    return None
                resp.raise_for_status()
                return await resp.read()

        log.error("onedrive.download_failed", item_id=item_id)
        return None
