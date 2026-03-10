"""
JobLifecycleService — accumulates per-document results and writes final job stats.

Design: stats accumulate in memory during the job (not one DB write per document).
One single MongoDB write happens at the end — efficient and atomic.
"""

from dataclasses import dataclass
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
    chunks_deleted: int = 0  # covers BOTH: old chunks from updates AND full deletions

    def record_document_result(self, result: dict) -> None:
        """Update counters from a single document's pipeline result."""
        self.documents_scanned += 1
        status = result.get("status", "failed")
        chunks = result.get("chunks", 0)
        chunks_deleted = result.get("chunks_deleted", 0)

        if status == "new":
            self.documents_new += 1
            self.chunks_created += chunks
        elif status == "updated":
            self.documents_updated += 1
            self.chunks_created += chunks
            self.chunks_deleted += chunks_deleted  # old chunks removed before re-embed

        elif status == "skipped":
            self.documents_skipped += 1
        elif status == "failed":
            self.documents_failed += 1

    def record_deletion(self, chunks_deleted: int = 0) -> None:
        self.documents_deleted += 1
        self.chunks_deleted += chunks_deleted

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
