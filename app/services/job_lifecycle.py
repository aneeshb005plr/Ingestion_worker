"""
JobLifecycleService — accumulates per-document results and writes final job stats.

Design: stats accumulate in memory during the job (not one DB write per document).
One single MongoDB write happens at the end — efficient and atomic.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import structlog

from app.repositories.job_repo import JobRepository

log = structlog.get_logger(__name__)


@dataclass
class JobStats:
    """Running totals accumulated during a job."""

    documents_scanned: int = 0
    documents_new: int = 0
    documents_updated: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0
    documents_deleted: int = 0
    chunks_created: int = 0
    chunks_deleted: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Document lists for full detail notifications
    # Each entry: { source_id, file_name, full_path, source_url, chunks, metadata }
    new_documents: list[dict] = field(default_factory=list)
    updated_documents: list[dict] = field(default_factory=list)
    deleted_documents: list[dict] = field(default_factory=list)
    error_documents: list[dict] = field(default_factory=list)

    def record_document_result(self, result: dict, raw_doc=None) -> None:
        """Update counters + document lists from a single document result."""
        self.documents_scanned += 1
        status = result.get("status", "failed")
        chunks = result.get("chunks", 0)
        chunks_deleted = result.get("chunks_deleted", 0)

        doc_info = self._build_doc_info(raw_doc, chunks=chunks) if raw_doc else {}

        if status == "new":
            self.documents_new += 1
            self.chunks_created += chunks
            if doc_info:
                self.new_documents.append(doc_info)
        elif status == "updated":
            self.documents_updated += 1
            self.chunks_created += chunks
            self.chunks_deleted += chunks_deleted
            if doc_info:
                self.updated_documents.append(
                    {**doc_info, "chunks_deleted": chunks_deleted}
                )
        elif status == "skipped":
            self.documents_skipped += 1
        elif status == "failed":
            self.documents_failed += 1
            if doc_info:
                self.error_documents.append(
                    {**doc_info, "error": result.get("error", "")}
                )

    def record_deletion(
        self, chunks_deleted: int = 0, raw_doc=None, source_id: str = ""
    ) -> None:
        """Called when a whole document is deleted."""
        self.documents_deleted += 1
        self.chunks_deleted += chunks_deleted
        doc_info = self._build_doc_info(raw_doc) if raw_doc else {}
        if not doc_info and source_id:
            doc_info = {"source_id": source_id}
        if doc_info:
            self.deleted_documents.append(doc_info)

    @staticmethod
    def _build_doc_info(raw_doc, chunks: int = 0) -> dict:
        """Extract document info for notification payload."""
        if raw_doc is None:
            return {}
        return {
            "source_id": getattr(raw_doc, "source_id", ""),
            "file_name": getattr(raw_doc, "file_name", ""),
            "full_path": getattr(raw_doc, "full_path", ""),
            "source_url": getattr(raw_doc, "source_url", ""),
            "chunks": chunks,
        }

    def to_dict(self) -> dict:
        return {
            "documents_scanned": self.documents_scanned,
            "documents_new": self.documents_new,
            "documents_updated": self.documents_updated,
            "documents_skipped": self.documents_skipped,
            "documents_failed": self.documents_failed,
            "documents_deleted": self.documents_deleted,
            "chunks_created": self.chunks_created,
            "chunks_deleted": self.chunks_deleted,
        }

    @property
    def has_failures(self) -> bool:
        return self.documents_failed > 0


class JobLifecycleService:

    def __init__(self, job_repo: JobRepository) -> None:
        self._repo = job_repo

    async def start(self, job_id: str) -> JobStats:
        """Mark job as processing, return a fresh stats accumulator."""
        await self._repo.mark_processing(job_id)
        return JobStats()

    async def complete(self, job_id: str, stats: JobStats) -> None:
        """
        Mark job as completed or partial, write final stats in one DB operation.
        partial = job ran but some documents had errors.
        """
        await self._repo.mark_completed(
            job_id=job_id,
            stats=stats.to_dict(),
            partial=stats.has_failures,
        )

    async def fail(self, job_id: str, error: str) -> None:
        """Mark job as failed due to a job-level (not document-level) error."""
        await self._repo.mark_failed(job_id, error)
