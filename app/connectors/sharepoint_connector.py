"""
SharePointConnector — Production Grade
Authentication: OAuth2 client credentials (application permissions, no user login)
Required Azure App Registration permissions: Sites.Read.All (Application)

Two modes:
  Full scan  (delta_token=None) — fetches all files, saves deltaLink
  Delta scan (delta_token=str)  — fetches only changed/deleted since last run

DeltaItem types yielded:
  type="upsert"  → new or changed file (raw_doc populated with content bytes)
  type="deleted" → file removed from SharePoint (source_id only)
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


class SharePointConnector(BaseConnector):
    """
    Connects to a SharePoint document library via Microsoft Graph API.

    connector_config keys (SharePointConnectorConfig):
      site_id                  — SharePoint site ID
      drive_id                 — Document library drive ID (None = default drive)
      root_folder              — Subfolder path to scope the scan (optional)
      file_extensions_include  — e.g. [".pdf", ".docx"] — empty = allow all
      file_extensions_exclude  — e.g. [".tmp"] — always skip these
      max_file_size_mb         — Skip files larger than this (default 50)

    credentials keys:
      tenant_id    — Azure AD tenant ID
      client_id    — App registration client ID
      client_secret — App registration client secret
    """

    supports_native_deletes = True  # We yield DeltaItem(type="deleted") from delta API

    def __init__(
        self,
        config: dict,
        credentials: dict,
        delta_token: Optional[str] = None,
    ) -> None:
        self._site_id: str = config["site_id"]
        self._drive_id: Optional[str] = config.get("drive_id")
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

        self._saved_delta_token: Optional[str] = delta_token  # from previous run
        self._new_delta_token: Optional[str] = None  # produced this run

        # Token cache — refresh 5 min before expiry
        self._access_token: str = ""
        self._token_expires_at: float = 0.0

        self._session: Optional[aiohttp.ClientSession] = None
        self._resolved_drive_id: Optional[str] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open HTTP session, validate credentials, resolve drive ID."""
        self._session = aiohttp.ClientSession()
        await self._refresh_token()  # fail fast if credentials are wrong
        if not self._drive_id:
            self._resolved_drive_id = await self._get_default_drive_id()
        else:
            self._resolved_drive_id = self._drive_id
        log.info(
            "sharepoint.connected",
            site_id=self._site_id,
            drive_id=self._resolved_drive_id,
            mode="delta" if self._saved_delta_token else "full",
        )

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # ── delta_token property — worker reads this after the run ────────────────

    @property
    def delta_token(self) -> Optional[str]:
        """
        The deltaLink produced by this run.
        Worker saves this via repo_config_repo.save_delta_token() after success.
        Next run will use this as the starting URL for delta scan.
        """
        return self._new_delta_token

    # ── Main fetch ─────────────────────────────────────────────────────────────

    async def fetch_documents(self) -> AsyncIterator[DeltaItem]:
        """
        Yield DeltaItems for all files.

        Full scan  (first run): all files in drive/folder.
        Delta scan (subsequent): only files changed/deleted since last run.

        File content is downloaded inline — raw_doc.content = bytes.
        Deleted items yield DeltaItem(type="deleted", source_id=..., raw_doc=None).
        """
        start_url = (
            self._saved_delta_token  # delta scan — use saved URL directly
            if self._saved_delta_token
            else self._build_delta_url()  # full scan
        )

        log.info(
            "sharepoint.scan_start",
            mode="delta" if self._saved_delta_token else "full",
            root_folder=self._root_folder or "/",
        )

        async for item in self._paginate(start_url):

            # ── Deleted item ───────────────────────────────────────────────────
            # Deleted items — per Microsoft Graph API docs:
            # driveItem delta returns deleted items with "deleted" facet.
            # { "id": "...", "deleted": {} }
            # NOT "@removed" — that is used by other Graph endpoints.
            # Worker Step 4 guards via deduplicator.mark_deleted():
            #   if existing_doc is None (file not in our scope) → skip safely
            if item.get("deleted") is not None:
                log.debug("sharepoint.item_deleted", item_id=item["id"])
                yield DeltaItem(type="deleted", source_id=item["id"])
                continue

            # ── Skip folders ───────────────────────────────────────────────────
            if "folder" in item:
                continue

            # ── Filter by root_folder path ────────────────────────────────────
            # Root-level delta returns ALL items in the drive.
            # We only want items under our configured root_folder.
            #
            # SharePoint parentReference.path after stripping drive prefix:
            #   "Shared Documents/docassist-test/XLOS/SPT/general"
            #
            # root_folder config: "docassist-test"
            #
            # We cannot use startswith() because SharePoint prepends
            # the document library name ("Shared Documents/") to the path.
            # Instead check if root_folder appears as a path segment.
            if self._root_folder:
                parent_path = (
                    item.get("parentReference", {})
                    .get("path", "")
                    .split("root:")[-1]
                    .strip("/")
                )
                item_path = (
                    f"{parent_path}/{item.get('name', '')}"
                    if parent_path
                    else item.get("name", "")
                )
                # Normalize both paths for comparison
                # Check if root_folder is a segment within the item path
                # e.g. "docassist-test" in "Shared Documents/docassist-test/XLOS/..."
                normalized_root = self._root_folder.strip("/")
                normalized_path = item_path.strip("/")
                if (
                    normalized_path != normalized_root
                    and f"/{normalized_root}/" not in f"/{normalized_path}/"
                    and not normalized_path.startswith(f"{normalized_root}/")
                ):
                    continue  # outside our root_folder scope — skip

            # ── Build and filter RawDocument ───────────────────────────────────
            raw_doc = await self._item_to_raw_document(item)
            if raw_doc is None:
                continue  # filtered — size, extension, hidden file

            yield DeltaItem(type="upsert", source_id=raw_doc.source_id, raw_doc=raw_doc)

    # ── Graph API ──────────────────────────────────────────────────────────────

    async def _refresh_token(self) -> None:
        url = TOKEN_URL.format(tenant_id=self._az_tenant_id)
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": GRAPH_SCOPE,
        }
        async with self._session.post(url, data=data) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"SharePoint auth failed ({resp.status}): {body}")
            result = await resp.json()

        self._access_token = result["access_token"]
        self._token_expires_at = time.time() + result.get("expires_in", 3600) - 300
        log.debug("sharepoint.token_refreshed", expires_in=result.get("expires_in"))

    async def _get_token(self) -> str:
        if time.time() >= self._token_expires_at:
            await self._refresh_token()
        return self._access_token

    async def _graph_get(self, url: str) -> dict:
        """Authenticated GET with retry on 401 and backoff on 429."""
        for attempt in range(3):
            token = await self._get_token()
            async with self._session.get(
                url, headers={"Authorization": f"Bearer {token}"}
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 401:
                    # Token rejected — force refresh and retry
                    self._token_expires_at = 0
                    continue
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", 10))
                    log.warning("sharepoint.rate_limited", retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                body = await resp.text()
                raise RuntimeError(f"Graph API {resp.status} on {url}: {body}")
        raise RuntimeError(f"Graph API request failed after retries: {url}")

    async def _paginate(self, start_url: str) -> AsyncIterator[dict]:
        """
        Follow @odata.nextLink pages, capture @odata.deltaLink on final page.
        Yields every driveItem from every page.
        """
        url: Optional[str] = start_url
        while url:
            data = await self._graph_get(url)
            for item in data.get("value", []):
                yield item

            if next_link := data.get("@odata.nextLink"):
                url = next_link
            elif delta_link := data.get("@odata.deltaLink"):
                # Final page — save deltaLink for next run
                self._new_delta_token = delta_link
                log.info("sharepoint.delta_link_captured")
                url = None
            else:
                url = None

    async def _get_default_drive_id(self) -> str:
        """Resolve the default document library drive ID for the site."""
        data = await self._graph_get(f"{GRAPH_BASE}/sites/{self._site_id}/drive")
        drive_id = data["id"]
        log.debug("sharepoint.drive_resolved", drive_id=drive_id)
        return drive_id

    # ── Item processing ────────────────────────────────────────────────────────

    def _build_delta_url(self) -> str:
        """
        Build the initial delta URL for a full scan.

        Always uses ROOT-LEVEL delta regardless of root_folder config.

        Why root-level instead of subfolder-scoped delta?
          Subfolder-scoped delta (/drives/{id}/root:/{folder}:/delta) has an
          inconsistency: deleted items inside the subfolder may not always
          appear with the "deleted" facet in the response.

          Root-level delta (/drives/{id}/root/delta) reliably returns
          the "deleted" facet for any deleted file across the drive.
          We filter by root_folder path in fetch_documents() instead.

          Per official docs: driveItem delta returns deleted items with
          "deleted" facet — { "id": "...", "deleted": {} }

        NOTE: No $select — @microsoft.graph.downloadUrl is an OData instance
        annotation not returned by the delta endpoint. Fetched per-item
        via a plain GET in _get_download_url().

        URL pattern: /drives/{drive-id}/root/delta
        Per Microsoft Q&A: /sites/{siteId}/drive/root/delta does NOT reliably
        return deleted items. /drives/{drive-id}/root/delta does.
        Reference: https://learn.microsoft.com/en-us/graph/api/driveitem-delta
        """
        return f"{GRAPH_BASE}/drives/{self._resolved_drive_id}/root/delta"

    async def _item_to_raw_document(self, item: dict) -> Optional[RawDocument]:
        """
        Convert a Graph driveItem to RawDocument.
        Downloads file content inline.
        Returns None if the item should be skipped.
        """
        name: str = item.get("name", "")

        # Skip Office temp files and hidden files
        if any(name.startswith(p) for p in HIDDEN_PREFIXES):
            log.debug("sharepoint.skip", name=name, reason="hidden")
            return None

        # Extension filters
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if self._extensions_include and ext not in self._extensions_include:
            log.debug("sharepoint.skip", name=name, reason="ext_not_included")
            return None
        if self._extensions_exclude and ext in self._extensions_exclude:
            log.debug("sharepoint.skip", name=name, reason="ext_excluded")
            return None

        # Size filter
        size: int = item.get("size", 0)
        if size > self._max_bytes:
            log.warning(
                "sharepoint.skip",
                name=name,
                size_mb=round(size / 1024 / 1024, 1),
                reason="too_large",
            )
            return None

        # Fetch download URL via separate GET — @microsoft.graph.downloadUrl
        # is an OData annotation not returned by delta endpoint
        item_id = item["id"]
        download_url = await self._get_download_url(item_id)
        if not download_url:
            log.warning("sharepoint.skip", name=name, reason="no_download_url")
            return None

        # Download content
        try:
            file_content = await self._download(download_url)
        except Exception as e:
            log.error("sharepoint.download_failed", name=name, error=str(e))
            return None

        # Content hash — use Graph SHA256 if available, else compute from bytes
        file_hashes = item.get("file", {}).get("hashes", {})
        content_hash = (
            file_hashes.get("sha256Hash")
            or file_hashes.get("sha1Hash")
            or hashlib.sha256(file_content).hexdigest()
        )

        # full_path — strip drive prefix from parentReference.path
        parent_path = (
            item.get("parentReference", {})
            .get("path", "")
            .split("root:")[-1]
            .strip("/")
        )
        full_path = f"{parent_path}/{name}" if parent_path else name

        # last_modified_at_source
        last_modified = None
        if modified_str := item.get("lastModifiedDateTime"):
            try:
                last_modified = datetime.fromisoformat(
                    modified_str.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        mime_type = item.get("file", {}).get("mimeType", "application/octet-stream")

        return RawDocument(
            source_id=item["id"],
            file_name=name,
            full_path=full_path,
            source_url=item.get("webUrl", ""),
            content_type="file",
            content=file_content,
            mime_type=mime_type,
            content_hash=content_hash,
            last_modified_at_source=last_modified,
            extra_metadata={
                "sharepoint_site_id": self._site_id,
                "sharepoint_drive_id": self._resolved_drive_id,
                "sharepoint_item_id": item["id"],
                "sharepoint_etag": item.get("eTag", ""),
                "size_bytes": size,
            },
        )

    async def _get_download_url(self, item_id: str) -> str | None:
        """
        Fetch @microsoft.graph.downloadUrl via a separate GET on the item.

        Why separate call:
          @microsoft.graph.downloadUrl is an OData instance annotation —
          NOT a standard driveItem property. The delta endpoint never returns
          it. A plain GET on the item returns it automatically without
          any $select parameter needed.

        Returns None if the URL cannot be obtained (item inaccessible etc.).
        """
        url = f"{GRAPH_BASE}/drives/{self._resolved_drive_id}" f"/items/{item_id}"
        try:
            data = await self._graph_get(url)
            return data.get("@microsoft.graph.downloadUrl")
        except Exception as e:
            log.warning(
                "sharepoint.download_url_fetch_failed", item_id=item_id, error=str(e)
            )
            return None

    async def _download(self, url: str) -> bytes:
        """Download file content from pre-authenticated Graph download URL."""
        async with self._session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Download failed ({resp.status})")
            return await resp.read()
