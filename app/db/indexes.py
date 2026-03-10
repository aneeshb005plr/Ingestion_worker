"""
MongoDB index creation for the worker.
The worker creates indexes for the collections it owns:
  - ingested_documents (delta registry)

The orchestrator handles ingestion_jobs and tenant/repo indexes.
Both services creating indexes is safe — create_index is idempotent.
"""

import structlog
from pymongo.asynchronous.database import AsyncDatabase

log = structlog.get_logger(__name__)


async def create_indexes(db: AsyncDatabase) -> None:
    log.info("mongodb.indexes.creating")

    # ingested_documents — the delta registry
    # Primary lookup: find a document by its source identity
    await db.ingested_documents.create_index(
        [("tenant_id", 1), ("repo_id", 1), ("source_id", 1)],
        unique=True,
    )
    # List all active docs for a repo (deleted-file detection)
    await db.ingested_documents.create_index([("repo_id", 1), ("status", 1)])
    # Hash lookup for content-based deduplication
    await db.ingested_documents.create_index("content_hash")

    log.info("mongodb.indexes.ready")
