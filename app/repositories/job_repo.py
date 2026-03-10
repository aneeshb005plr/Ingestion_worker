"""
JobRepository (Worker side) — updates job status and stats.

The orchestrator creates job records. The worker updates them.
This repo only has UPDATE methods — no INSERT.
"""

from datetime import datetime, timezone

import structlog
from pymongo.asynchronous.database import AsyncDatabase

from app.repositories.base import BaseRepository

log = structlog.get_logger(__name__)


class JobRepository(BaseRepository):

    COLLECTION = "ingestion_jobs"

    def __init__(self, db: AsyncDatabase) -> None:
        super().__init__(db)
        self.collection = db[self.COLLECTION]

    async def mark_processing(self, job_id: str) -> None:
        """Called at the start of a task — job is now being worked on."""
        await self.collection.update_one(
            {"job_id": job_id},
            {
                "$set": {
                    "status": "processing",
                    "started_at": datetime.now(timezone.utc),
                }
            },
        )
        log.info("job.processing", job_id=job_id)

    async def mark_completed(
        self, job_id: str, stats: dict, partial: bool = False
    ) -> None:
        """
         Called on successful task completion.
        Writes the final stats in a single DB write — not per document.

        partial=True → status="partial" (job ran but some documents had errors)
        partial=False → status="completed" (all documents processed cleanly)
        """
        now = datetime.now(timezone.utc)
        final_status = "partial" if partial else "completed"

        # Calculate duration from the job's started_at
        job = await self.collection.find_one({"job_id": job_id}, {"started_at": 1})
        duration = None
        if job and job.get("started_at"):
            duration = int((now - job["started_at"]).total_seconds())

        await self.collection.update_one(
            {"job_id": job_id},
            {
                "$set": {
                    "status": final_status,
                    "completed_at": now,
                    "duration_seconds": duration,
                    "stats": stats,
                }
            },
        )
        log.info(
            "job.completed",
            job_id=job_id,
            status=final_status,
            duration_seconds=duration,
            **stats
        )

    async def mark_failed(self, job_id: str, error: str) -> None:
        """Called when the task hits an unrecoverable error."""
        await self.collection.update_one(
            {"job_id": job_id},
            {
                "$set": {
                    "status": "failed",
                    "completed_at": datetime.now(timezone.utc),
                    "errors": [{"type": "job_level_error", "error": error}],
                }
            },
        )
        log.error("job.failed", job_id=job_id, error=error)

    async def append_document_error(
        self, job_id: str, file_name: str, error: str
    ) -> None:
        """
        Append a per-document error to the job record.
        The job continues — this is not a job-level failure.
        """
        await self.collection.update_one(
            {"job_id": job_id},
            {"$push": {"errors": {"file": file_name, "error": error}}},
        )
