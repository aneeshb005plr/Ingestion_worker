"""
DocumentRepository — the delta registry.

Tracks every document that has been ingested:
  - Used to detect new vs changed vs unchanged files
  - Used to find deleted files (in registry but not in latest scan)
  - Stores content hash, chunk count, full path, source URL
"""

from datetime import datetime, timezone
from typing import Literal

import structlog
from pymongo.asynchronous.database import AsyncDatabase

from app.repositories.base import BaseRepository

log = structlog.get_logger(__name__)

DocumentStatus = Literal["active", "deleted", "skipped"]


class DocumentRepository(BaseRepository):

    def __init__(self, db: AsyncDatabase) -> None:
        super().__init__(db)
        self.collection = db["ingested_documents"]

    async def get_by_source_id(
        self, tenant_id: str, repo_id: str, source_id: str
    ) -> dict | None:
        """
        Look up a document by its source identity.
        Returns None if this document has never been ingested before (it's new).
        """
        return await self.collection.find_one(
            {
                "tenant_id": tenant_id,
                "repo_id": repo_id,
                "source_id": source_id,
            }
        )

    async def upsert(
        self,
        tenant_id: str,
        repo_id: str,
        source_id: str,
        job_id: str,
        file_name: str,
        full_path: str,
        source_url: str,
        mime_type: str,
        content_hash: str,
        last_modified_at_source: datetime | None,
        chunk_count: int,
        status: DocumentStatus = "active",
    ) -> None:
        """
        Insert or update a document registry record.
        Called after successful ingestion of a document.
        """
        now = datetime.now(timezone.utc)
        await self.collection.update_one(
            # Match on the source identity (unique index)
            {"tenant_id": tenant_id, "repo_id": repo_id, "source_id": source_id},
            {
                "$set": {
                    "job_id": job_id,
                    "file_name": file_name,
                    "full_path": full_path,
                    "source_url": source_url,
                    "mime_type": mime_type,
                    "content_hash": content_hash,
                    "last_modified_at_source": last_modified_at_source,
                    "last_ingested_at": now,
                    "chunk_count": chunk_count,
                    "status": status,
                }
            },
            upsert=True,  # insert if not exists, update if exists
        )

    async def mark_deleted(self, tenant_id: str, repo_id: str, source_id: str) -> None:
        """
        Mark a document as deleted.
        Called when a file no longer exists in the source repository.
        The vector chunks are deleted separately by the vector store provider.
        """
        await self.collection.update_one(
            {"tenant_id": tenant_id, "repo_id": repo_id, "source_id": source_id},
            {
                "$set": {
                    "status": "deleted",
                    "last_ingested_at": datetime.now(timezone.utc),
                }
            },
        )

    async def get_all_active_source_ids(self, tenant_id: str, repo_id: str) -> set[str]:
        """
        Returns the set of source_ids currently marked active for a repo.
        Used for deleted-file detection: compare against what the connector returned.
        Files in this set but NOT in the connector's output → deleted.
        """
        cursor = self.collection.find(
            {"tenant_id": tenant_id, "repo_id": repo_id, "status": "active"},
            {"source_id": 1},
        )
        docs = await cursor.to_list(length=None)
        return {doc["source_id"] for doc in docs}
