"""
LocalConnector — reads files from a local filesystem directory.

Used for development and testing. Not intended for production.
Supports content-hash based delta detection (no native delta API).

supports_native_deletes = False (default)
  → Worker detects deleted files in Step 5 by comparing seen_source_ids
    against the ingested_documents registry.

Config keys (connector_config):
  directory_path          : str  — root directory to scan
  recursive               : bool — scan subdirectories (default: True)
  file_extensions_include : list — e.g. [".pdf", ".docx"] (empty = all supported)
  file_extensions_exclude : list — e.g. [".tmp", ".log"]
"""

import hashlib
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import aiofiles
import structlog

from app.connectors.base import BaseConnector, DeltaItem, RawDocument
from app.core.exceptions import ConnectorError

log = structlog.get_logger(__name__)


class LocalConnector(BaseConnector):

    # supports_native_deletes = False (inherited default)
    # Worker will compare seen_source_ids against registry after the run.

    def __init__(self, config: dict, credentials: dict) -> None:
        # credentials not needed for local — accepted for interface consistency
        self.root_path = Path(config["directory_path"])
        self.recursive = config.get("recursive", True)
        self.include_extensions: set[str] = {
            ext.lower() for ext in config.get("file_extensions_include", [])
        }
        self.exclude_extensions: set[str] = {
            ext.lower()
            for ext in config.get("file_extensions_exclude", [".tmp", ".log"])
        }

    async def connect(self) -> None:
        """Verify the directory exists and is readable."""
        if not self.root_path.exists():
            raise ConnectorError(f"Local directory not found: {self.root_path}")
        if not self.root_path.is_dir():
            raise ConnectorError(f"Path is not a directory: {self.root_path}")
        log.info("connector.local.connected", path=str(self.root_path))

    async def fetch_documents(self) -> AsyncGenerator[DeltaItem, None]:
        """
        Yield DeltaItems for all matching files in the directory.
        Only yields type="upsert" — deletion detection is handled by the
        worker comparing seen_source_ids against the registry after this loop.
        """
        pattern = "**/*" if self.recursive else "*"

        for file_path in sorted(self.root_path.glob(pattern)):
            if not file_path.is_file():
                continue

            ext = file_path.suffix.lower()

            # Apply extension filters
            if self.include_extensions and ext not in self.include_extensions:
                continue
            if ext in self.exclude_extensions:
                continue

            # Skip hidden and Office temp files
            if file_path.name.startswith((".", "~$")):
                continue

            try:
                async with aiofiles.open(file_path, "rb") as f:
                    content = await f.read()

                content_hash = hashlib.sha256(content).hexdigest()
                mime_type, _ = mimetypes.guess_type(file_path.name)

                # Use relative path from root as the stable source_id
                full_path = "/" + str(file_path.relative_to(self.root_path)).replace(
                    "\\", "/"
                )

                stat = file_path.stat()
                last_modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

                yield DeltaItem(
                    type="upsert",
                    source_id=full_path,
                    raw_doc=RawDocument(
                        content_type="file",
                        content=content,
                        mime_type=mime_type or "application/octet-stream",
                        source_id=full_path,
                        file_name=file_path.name,
                        full_path=full_path,
                        source_url=file_path.as_uri(),
                        content_hash=content_hash,
                        last_modified_at_source=last_modified,
                    ),
                )

            except Exception as e:
                # Per-file error isolation — log and continue, don't fail the job
                log.warning(
                    "connector.local.file_error",
                    file=str(file_path),
                    error=str(e),
                )
                continue

    async def disconnect(self) -> None:
        pass  # nothing to close for local filesystem
