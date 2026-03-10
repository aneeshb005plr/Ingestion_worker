"""
Deduplicator — decides what to do with each document in a delta scan.

Decision logic per document:
  NEW       → source_id not in registry → ingest
  CHANGED   → source_id in registry, content hash differs → delete old chunks, re-ingest
  UNCHANGED → source_id in registry, content hash matches → skip

Note on DELETED:
  Deletion is NOT handled here. It is a repo-level concern, not a document-level one.
  You cannot "dedup check" a deleted file — it no longer exists to hash.
  Deletion is handled in worker.py in two ways:
    1. Connector-reported deletes (SharePoint delta API) — delta_item.type="deleted"
    2. Registry-compared deletes (local, SQL, MongoDB) — seen_source_ids vs registered_ids
  The deduplicator only operates on documents that ARE present in the source.
"""

from dataclasses import dataclass
from typing import Literal

import structlog

from app.repositories.document_repo import DocumentRepository

log = structlog.get_logger(__name__)

# Only three valid decisions for a document that exists in the source
Decision = Literal["new", "changed", "unchanged"]


@dataclass
class DedupResult:
    decision: Decision
    existing_doc: dict | None = None  # the registry record if it exists


class Deduplicator:

    def __init__(self, document_repo: DocumentRepository) -> None:
        self._repo = document_repo

    async def check(
        self,
        tenant_id: str,
        repo_id: str,
        source_id: str,
        content_hash: str,
        last_modified_at_source=None,
    ) -> DedupResult:
        """
        Check the registry and return what action to take for this document.

        Args:
            tenant_id:               tenant identifier
            repo_id:                 repository identifier
            source_id:               unique ID of the document within the repo
            content_hash:            sha256 of the document content
            last_modified_at_source: optional timestamp from the source system

        Returns:
            DedupResult with decision: "new", "changed", or "unchanged"
        """
        existing = await self._repo.get_by_source_id(
            tenant_id=tenant_id,
            repo_id=repo_id,
            source_id=source_id,
        )

        if existing is None:
            log.debug("dedup.new", source_id=source_id)
            return DedupResult(decision="new")

        if existing.get("content_hash") == content_hash:
            log.debug("dedup.unchanged", source_id=source_id)
            return DedupResult(decision="unchanged", existing_doc=existing)

        log.debug("dedup.changed", source_id=source_id)
        return DedupResult(decision="changed", existing_doc=existing)

    async def mark_deleted(
        self,
        tenant_id: str,
        repo_id: str,
        source_id: str,
    ) -> DedupResult:
        """
        Check whether a document exists in the registry before deletion.

        Used in Step 4 (connector-reported deletes, e.g. SharePoint delta API).
        Returns DedupResult so the caller can guard: only delete vectors if
        existing_doc is not None — i.e. this file was actually ingested before.

        Does NOT modify the registry — the worker calls document_repo.mark_deleted()
        separately after confirming the vector deletion succeeded.

        Returns:
            DedupResult(decision="unchanged", existing_doc=<doc>) if found in registry
            DedupResult(decision="new",       existing_doc=None)   if not in registry
        """
        existing = await self._repo.get_by_source_id(
            tenant_id=tenant_id,
            repo_id=repo_id,
            source_id=source_id,
        )
        if existing is None:
            log.debug(
                "dedup.delete_skipped_not_ingested",
                source_id=source_id,
                reason="document was never ingested — deletion ignored",
            )
            return DedupResult(decision="new", existing_doc=None)

        log.debug("dedup.delete_confirmed", source_id=source_id)
        return DedupResult(decision="unchanged", existing_doc=existing)

    async def check_for_deletions(
        self,
        tenant_id: str,
        repo_id: str,
        seen_source_ids: set[str],
    ) -> list[str]:
        """
        Find documents previously ingested but NOT seen in the latest scan.

        Used in Step 5 for connectors without native delete support
        (local filesystem, SQL, MongoDB).

        Compares the registry's active source_ids against what the connector
        yielded this run. Anything registered but absent from seen_source_ids
        means the file no longer exists at source → should be deleted.

        Returns:
            List of source_ids to delete (may be empty)
        """
        registered_ids = await self._repo.get_all_active_source_ids(
            tenant_id=tenant_id,
            repo_id=repo_id,
        )

        deleted_ids = list(registered_ids - seen_source_ids)

        if deleted_ids:
            log.info(
                "dedup.deletions_detected",
                tenant_id=tenant_id,
                repo_id=repo_id,
                count=len(deleted_ids),
                source_ids=deleted_ids,
            )
        else:
            log.debug(
                "dedup.no_deletions",
                tenant_id=tenant_id,
                repo_id=repo_id,
                registered=len(registered_ids),
                seen=len(seen_source_ids),
            )

        return deleted_ids
