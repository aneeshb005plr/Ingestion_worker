"""
WebhookNotifier — sends ingestion notifications via HTTP POST.

Features:
  - All auth types via AuthHandler
  - Retry with exponential backoff (default 3 attempts)
  - Configurable timeout (default 10s)
  - Structured logging per attempt
"""

import asyncio
import json
import structlog
import httpx

from app.notifications.auth_handler import AuthHandler

log = structlog.get_logger(__name__)


class WebhookNotifier:

    def __init__(self) -> None:
        self._auth_handler = AuthHandler()

    async def send(
        self,
        channel: dict,
        payload: dict,
    ) -> bool:
        """
        POST notification payload to webhook URL.

        Args:
            channel: webhook channel config from notification_config
            payload: notification payload from PayloadBuilder

        Returns:
            True if delivered successfully, False after all retries exhausted.
        """
        url = channel.get("url", "")
        auth_config = channel.get("auth", {"type": "none"})
        timeout = channel.get("timeout_seconds", 10)
        max_retries = channel.get("max_retries", 3)

        if not url:
            log.warning("webhook_notifier.no_url")
            return False

        body_bytes = json.dumps(payload, default=str).encode("utf-8")

        # Resolve auth headers (may involve token fetch)
        try:
            auth_headers = await self._auth_handler.get_headers(auth_config, body_bytes)
        except Exception as e:
            log.error("webhook_notifier.auth_failed", url=url, error=str(e))
            return False

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "VectorPlatform-Notifier/1.0",
            **auth_headers,
        }

        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        url, content=body_bytes, headers=headers
                    )

                if response.status_code < 300:
                    log.info(
                        "webhook_notifier.sent",
                        url=url,
                        status=response.status_code,
                        attempt=attempt,
                        repo_id=payload.get("repo_id"),
                    )
                    return True

                # 4xx — don't retry (bad request, auth error)
                if 400 <= response.status_code < 500:
                    log.error(
                        "webhook_notifier.client_error",
                        url=url,
                        status=response.status_code,
                        body=response.text[:200],
                    )
                    return False

                # 5xx — retry
                log.warning(
                    "webhook_notifier.server_error",
                    url=url,
                    status=response.status_code,
                    attempt=attempt,
                    max_retries=max_retries,
                )

            except httpx.TimeoutException:
                log.warning(
                    "webhook_notifier.timeout",
                    url=url,
                    attempt=attempt,
                    timeout=timeout,
                )
            except Exception as e:
                log.warning(
                    "webhook_notifier.error",
                    url=url,
                    attempt=attempt,
                    error=str(e),
                )

            # Exponential backoff: 2s, 4s, 8s
            if attempt < max_retries:
                wait = 2**attempt
                log.debug("webhook_notifier.retry_wait", seconds=wait, attempt=attempt)
                await asyncio.sleep(wait)

        log.error(
            "webhook_notifier.all_retries_exhausted",
            url=url,
            max_retries=max_retries,
        )
        return False
