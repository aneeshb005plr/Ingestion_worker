"""
NotificationService — orchestrates all notification channels after job completion.

Flow:
  1. Check master switch (notification_config.enabled)
  2. Check trigger conditions (always / on_failure / on_change)
  3. For each enabled channel:
       - Build payload (summary or full)
       - Dispatch (email or webhook)
       - Log result (never raise — notification failure must not fail the job)

Design principles:
  - Never raises — all exceptions caught and logged
  - Channels dispatched concurrently (asyncio.gather)
  - Trigger evaluation is strict:
      always     → always send
      on_failure → documents_failed > 0 OR status == "failed"
      on_change  → documents_new + documents_updated + documents_deleted > 0
  - Multiple triggers are ORed — send if ANY trigger matches
"""

import asyncio
import structlog

from app.notifications.payload_builder import PayloadBuilder
from app.notifications.email_notifier import EmailNotifier
from app.notifications.webhook_notifier import WebhookNotifier
from app.services.job_lifecycle import JobStats

log = structlog.get_logger(__name__)


class NotificationService:

    def __init__(self) -> None:
        self._payload_builder = PayloadBuilder()
        self._email_notifier = EmailNotifier()
        self._webhook_notifier = WebhookNotifier()

    async def notify(
        self,
        notification_config: dict,
        job_id: str,
        tenant_id: str,
        repo_id: str,
        repo_name: str,
        trigger: str,
        status: str,
        stats: JobStats,
        error: str | None = None,
    ) -> None:
        """
        Dispatch notifications for a completed job.
        Never raises — all failures are logged.

        Args:
            notification_config: repo.notification_config dict from MongoDB
            trigger:             "manual" | "scheduled"
            status:              "completed" | "partial" | "failed"
            error:               job-level error message (status=failed only)
        """
        try:
            await self._dispatch(
                notification_config=notification_config,
                job_id=job_id,
                tenant_id=tenant_id,
                repo_id=repo_id,
                repo_name=repo_name,
                trigger=trigger,
                status=status,
                stats=stats,
                error=error,
            )
        except Exception as e:
            log.error(
                "notification_service.unexpected_error",
                job_id=job_id,
                repo_id=repo_id,
                error=str(e),
            )

    async def _dispatch(
        self,
        notification_config: dict,
        job_id: str,
        tenant_id: str,
        repo_id: str,
        repo_name: str,
        trigger: str,
        status: str,
        stats: JobStats,
        error: str | None,
    ) -> None:

        # ── Step 1: Master switch ─────────────────────────────────────────────
        if not notification_config.get("enabled", False):
            log.debug("notification_service.disabled", repo_id=repo_id)
            return

        channels = notification_config.get("channels", [])
        if not channels:
            log.debug("notification_service.no_channels", repo_id=repo_id)
            return

        # ── Step 2: Trigger check ─────────────────────────────────────────────
        triggers = notification_config.get("triggers", ["always"])
        if not self._should_notify(triggers, status, stats):
            log.debug(
                "notification_service.trigger_not_met",
                repo_id=repo_id,
                triggers=triggers,
                status=status,
            )
            return

        log.info(
            "notification_service.dispatching",
            repo_id=repo_id,
            job_id=job_id,
            channels=len(channels),
            triggers=triggers,
            status=status,
        )

        # ── Step 3: Dispatch all enabled channels concurrently ────────────────
        tasks = []
        for channel in channels:
            if not channel.get("enabled", True):
                continue

            channel_type = channel.get("type")
            detail_level = channel.get("detail_level", "summary")

            payload = self._payload_builder.build(
                detail_level=detail_level,
                job_id=job_id,
                tenant_id=tenant_id,
                repo_id=repo_id,
                repo_name=repo_name,
                trigger=trigger,
                status=status,
                stats=stats,
                error=error,
            )

            if channel_type == "email":
                tasks.append(self._send_email(channel, payload))
            elif channel_type == "webhook":
                tasks.append(self._send_webhook(channel, payload))
            else:
                log.warning(
                    "notification_service.unknown_channel_type",
                    channel_type=channel_type,
                )

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            successes = sum(1 for r in results if r is True)
            log.info(
                "notification_service.done",
                repo_id=repo_id,
                job_id=job_id,
                total=len(tasks),
                succeeded=successes,
                failed=len(tasks) - successes,
            )

    def _should_notify(
        self,
        triggers: list[str],
        status: str,
        stats: JobStats,
    ) -> bool:
        """
        Evaluate trigger conditions — ORed together.
        Returns True if ANY trigger condition is met.
        """
        for trigger in triggers:
            if trigger == "always":
                return True

            if trigger == "on_failure":
                if status == "failed" or stats.documents_failed > 0:
                    return True

            if trigger == "on_change":
                changed = (
                    stats.documents_new
                    + stats.documents_updated
                    + stats.documents_deleted
                )
                if changed > 0:
                    return True

        return False

    async def _send_email(self, channel: dict, payload: dict) -> bool:
        try:
            return await self._email_notifier.send(channel, payload)
        except Exception as e:
            log.error(
                "notification_service.email_error",
                repo_id=payload.get("repo_id"),
                error=str(e),
            )
            return False

    async def _send_webhook(self, channel: dict, payload: dict) -> bool:
        try:
            return await self._webhook_notifier.send(channel, payload)
        except Exception as e:
            log.error(
                "notification_service.webhook_error",
                repo_id=payload.get("repo_id"),
                error=str(e),
            )
            return False
