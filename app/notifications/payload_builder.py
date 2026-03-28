"""
PayloadBuilder — builds notification payload from job stats.

Two detail levels:
  summary — job stats only (counts, duration, status)
  full    — summary + document lists (new/updated/deleted/errors)

Same base structure for both email and webhook.
Email uses subset of fields, webhook gets everything.
"""

from datetime import datetime, timezone
from app.services.job_lifecycle import JobStats


class PayloadBuilder:

    def build(
        self,
        detail_level: str,
        job_id: str,
        tenant_id: str,
        repo_id: str,
        repo_name: str,
        trigger: str,
        status: str,
        stats: JobStats,
        completed_at: datetime | None = None,
        error: str | None = None,
    ) -> dict:
        """
        Build notification payload.

        Args:
            detail_level: "summary" or "full"
            status:       "completed" | "partial" | "failed"
            error:        job-level error message (status=failed only)
        """
        now = completed_at or datetime.now(timezone.utc)
        duration = int((now - stats.started_at).total_seconds())

        payload = {
            "event": "ingestion.completed",
            "tenant_id": tenant_id,
            "repo_id": repo_id,
            "repo_name": repo_name,
            "job_id": job_id,
            "trigger": trigger,
            "status": status,
            "triggered_at": stats.started_at.isoformat(),
            "completed_at": now.isoformat(),
            "duration_seconds": duration,
            "summary": {
                "documents_scanned": stats.documents_scanned,
                "documents_new": stats.documents_new,
                "documents_updated": stats.documents_updated,
                "documents_skipped": stats.documents_skipped,
                "documents_deleted": stats.documents_deleted,
                "documents_failed": stats.documents_failed,
                "chunks_created": stats.chunks_created,
                "chunks_deleted": stats.chunks_deleted,
            },
        }

        if error:
            payload["error"] = error

        if detail_level == "full":
            payload["documents"] = {
                "new": stats.new_documents,
                "updated": stats.updated_documents,
                "deleted": stats.deleted_documents,
                "errors": stats.error_documents,
            }

        return payload
