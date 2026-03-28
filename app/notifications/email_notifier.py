"""
EmailNotifier — sends ingestion notifications via SendGrid.

Supports:
  - SendGrid dynamic templates (branded, designed in SendGrid dashboard)
  - Plain HTML fallback (when no template_id configured)

Template variables passed to SendGrid dynamic templates:
  All fields from the notification payload are passed as
  dynamic_template_data — template uses {{ variable }} syntax.

SendGrid API docs:
  POST https://api.sendgrid.com/v3/mail/send
"""

import json
import structlog
import httpx

from app.core.config import settings

log = structlog.get_logger(__name__)

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"

# Status → emoji for plain HTML email
STATUS_EMOJI = {
    "completed": "✅",
    "partial": "⚠️",
    "failed": "❌",
}


class EmailNotifier:

    async def send(
        self,
        channel: dict,
        payload: dict,
    ) -> bool:
        """
        Send email notification via SendGrid.

        Args:
            channel: email channel config from notification_config
            payload: notification payload from PayloadBuilder

        Returns:
            True if sent successfully, False on failure.
        """
        if not settings.SENDGRID_API_KEY:
            log.warning(
                "email_notifier.no_api_key",
                msg="SENDGRID_API_KEY not set — skipping email",
            )
            return False

        recipients = channel.get("recipients", [])
        if not recipients:
            log.warning("email_notifier.no_recipients")
            return False

        from_email = channel.get("from_email") or settings.SENDGRID_FROM_EMAIL
        from_name = channel.get("from_name") or settings.SENDGRID_FROM_NAME
        detail_level = channel.get("detail_level", "summary")

        # Template selected from platform .env based on detail_level
        # Tenants never configure template IDs — those belong to our SendGrid account
        template_id = (
            settings.SENDGRID_TEMPLATE_FULL
            if detail_level == "full"
            else settings.SENDGRID_TEMPLATE_SUMMARY
        )

        subject = self._build_subject(payload)

        message = {
            "personalizations": [{"to": [{"email": r} for r in recipients]}],
            "from": {"email": from_email, "name": from_name},
        }

        if template_id:
            # SendGrid dynamic template
            message["template_id"] = template_id
            message["personalizations"][0]["dynamic_template_data"] = payload
        else:
            # Plain HTML fallback
            message["subject"] = subject
            message["content"] = [
                {"type": "text/plain", "value": self._build_plain_text(payload)},
                {"type": "text/html", "value": self._build_html(payload)},
            ]

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    SENDGRID_API_URL,
                    headers={
                        "Authorization": f"Bearer {settings.SENDGRID_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    content=json.dumps(message),
                )
                response.raise_for_status()

            log.info(
                "email_notifier.sent",
                repo_id=payload.get("repo_id"),
                recipients=len(recipients),
                template=bool(template_id),
            )
            return True

        except Exception as e:
            log.error(
                "email_notifier.failed",
                repo_id=payload.get("repo_id"),
                error=str(e),
            )
            return False

    def _build_subject(self, payload: dict) -> str:
        status = payload.get("status", "unknown")
        repo_name = payload.get("repo_name", payload.get("repo_id", ""))
        emoji = STATUS_EMOJI.get(status, "ℹ️")
        return f"{emoji} Ingestion {status} — {repo_name}"

    def _build_plain_text(self, payload: dict) -> str:
        summary = payload.get("summary", {})
        lines = [
            f"Ingestion {payload.get('status', '').upper()}",
            f"Repo:      {payload.get('repo_name', payload.get('repo_id', ''))}",
            f"Job ID:    {payload.get('job_id', '')}",
            f"Trigger:   {payload.get('trigger', '')}",
            f"Duration:  {payload.get('duration_seconds', 0)}s",
            "",
            "Results:",
            f"  New:      {summary.get('documents_new', 0)}",
            f"  Updated:  {summary.get('documents_updated', 0)}",
            f"  Deleted:  {summary.get('documents_deleted', 0)}",
            f"  Skipped:  {summary.get('documents_skipped', 0)}",
            f"  Failed:   {summary.get('documents_failed', 0)}",
            f"  Chunks:   {summary.get('chunks_created', 0)} created",
        ]

        if payload.get("error"):
            lines += ["", f"Error: {payload['error']}"]

        docs = payload.get("documents", {})
        if docs.get("errors"):
            lines += ["", "Document Errors:"]
            for doc in docs["errors"]:
                lines.append(f"  - {doc.get('file_name', '')}: {doc.get('error', '')}")

        return "\n".join(lines)

    def _build_html(self, payload: dict) -> str:
        summary = payload.get("summary", {})
        status = payload.get("status", "unknown")
        emoji = STATUS_EMOJI.get(status, "ℹ️")
        repo_name = payload.get("repo_name", payload.get("repo_id", ""))

        rows = [
            ("New documents", summary.get("documents_new", 0)),
            ("Updated documents", summary.get("documents_updated", 0)),
            ("Deleted documents", summary.get("documents_deleted", 0)),
            ("Skipped documents", summary.get("documents_skipped", 0)),
            ("Failed documents", summary.get("documents_failed", 0)),
            ("Chunks created", summary.get("chunks_created", 0)),
            ("Duration", f"{payload.get('duration_seconds', 0)}s"),
        ]

        table_rows = "\n".join(
            [
                f"<tr><td style='padding:4px 12px;color:#666'>{k}</td>"
                f"<td style='padding:4px 12px;font-weight:bold'>{v}</td></tr>"
                for k, v in rows
            ]
        )

        error_section = ""
        if payload.get("error"):
            error_section = (
                f"<p style='color:#d32f2f;background:#fde8e8;padding:12px;border-radius:4px'>"
                f"<strong>Error:</strong> {payload['error']}</p>"
            )

        doc_errors = payload.get("documents", {}).get("errors", [])
        doc_error_section = ""
        if doc_errors:
            error_rows = "\n".join(
                [
                    f"<tr><td style='padding:4px 8px'>{d.get('file_name','')}</td>"
                    f"<td style='padding:4px 8px;color:#d32f2f'>{d.get('error','')}</td></tr>"
                    for d in doc_errors
                ]
            )
            doc_error_section = (
                f"<h3>Document Errors</h3>"
                f"<table border='0' cellspacing='0' style='width:100%'>"
                f"<tr><th style='text-align:left'>File</th><th style='text-align:left'>Error</th></tr>"
                f"{error_rows}</table>"
            )

        return f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto">
  <h2>{emoji} Ingestion {status.capitalize()} — {repo_name}</h2>
  <p style="color:#666">Job ID: {payload.get('job_id', '')} | 
     Trigger: {payload.get('trigger', '')} | 
     Completed: {payload.get('completed_at', '')}</p>
  <table border="0" cellspacing="0" style="width:100%;background:#f5f5f5;border-radius:4px">
    {table_rows}
  </table>
  {error_section}
  {doc_error_section}
</div>
"""
