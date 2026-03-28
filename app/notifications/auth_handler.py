"""
AuthHandler — resolves webhook authentication per auth type.

Handles all auth types:
  none           → no headers added
  api_key        → X-Api-Key: {value}
  bearer_token   → Authorization: Bearer {token}
  custom_headers → arbitrary static headers
  hmac           → X-Signature: sha256={hash} (computed over body)
  oauth2         → dynamic token via client credentials
                   primary + optional secondary token

Token caching (oauth2):
  Tokens cached in memory per (token_url, client_id) key.
  Cache respects expiry — refreshed 60s before expiry.
  Per-task cache: discarded after job completes.
  Simple dict cache — asyncio single-threaded, no locks needed.
"""

import hashlib
import hmac as hmac_lib
import json
import time
import structlog
import httpx
from app.core.encryption import decrypt_value as _decrypt_value

log = structlog.get_logger(__name__)

# In-memory token cache: key → (access_token, expires_at)
_token_cache: dict[str, tuple[str, float]] = {}


async def decrypt_value(value: str) -> str:
    """Async wrapper around encryption module decrypt_value."""
    return await _decrypt_value(value)


class AuthHandler:

    async def get_headers(
        self,
        auth_config: dict,
        body_bytes: bytes,
    ) -> dict[str, str]:
        """
        Build auth headers for a webhook request.

        Args:
            auth_config: auth dict from notification_config channel
            body_bytes:  serialized request body (for HMAC signing)

        Returns:
            dict of headers to add to the request
        """
        auth_type = auth_config.get("type", "none")

        if auth_type == "none":
            return {}

        if auth_type == "api_key":
            return await self._api_key_headers(auth_config)

        if auth_type == "bearer_token":
            return await self._bearer_token_headers(auth_config)

        if auth_type == "custom_headers":
            return await self._custom_headers(auth_config)

        if auth_type == "hmac":
            return self._hmac_headers(auth_config, body_bytes)

        if auth_type == "oauth2":
            return await self._oauth2_headers(auth_config)

        log.warning("auth_handler.unknown_type", auth_type=auth_type)
        return {}

    # ── Auth type implementations ─────────────────────────────────────────────

    async def _api_key_headers(self, auth: dict) -> dict:
        header_name = auth.get("header_name", "X-Api-Key")
        value = decrypt_value(auth.get("value", ""))
        return {header_name: value}

    async def _bearer_token_headers(self, auth: dict) -> dict:
        token = decrypt_value(auth.get("token", ""))
        return {"Authorization": f"Bearer {token}"}

    async def _custom_headers(self, auth: dict) -> dict:
        raw_headers = auth.get("headers", {})
        return {k: decrypt_value(v) for k, v in raw_headers.items()}

    def _hmac_headers(self, auth: dict, body_bytes: bytes) -> dict:
        secret = decrypt_value(auth.get("secret", ""))
        signature = hmac_lib.new(
            secret.encode("utf-8"),
            body_bytes,
            hashlib.sha256,
        ).hexdigest()
        header_name = auth.get("header_name", "X-Signature")
        return {header_name: f"sha256={signature}"}

    async def _oauth2_headers(self, auth: dict) -> dict:
        headers = {}

        # Primary token
        primary_token = await self._fetch_token(
            token_url=auth["token_url"],
            client_id=auth["client_id"],
            client_secret=decrypt_value(auth["client_secret"]),
            scope=auth.get("scope"),
        )
        header_name = auth.get("header_name", "Authorization")
        headers[header_name] = f"Bearer {primary_token}"

        # Secondary token (optional — e.g. Apigee + Entra combined)
        secondary = auth.get("secondary_auth")
        if secondary:
            secondary_token = await self._fetch_token(
                token_url=secondary["token_url"],
                client_id=secondary["client_id"],
                client_secret=decrypt_value(secondary["client_secret"]),
                scope=secondary.get("scope"),
            )
            sec_header = secondary.get("header_name", "X-Secondary-Token")
            headers[sec_header] = f"Bearer {secondary_token}"

        return headers

    async def _fetch_token(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None,
    ) -> str:
        """
        Fetch OAuth2 token via client credentials flow.
        Caches token until 60 seconds before expiry.
        """
        cache_key = f"{token_url}:{client_id}"
        cached = _token_cache.get(cache_key)
        if cached:
            token, expires_at = cached
            if time.time() < expires_at - 60:  # 60s buffer
                log.debug("auth_handler.token_cache_hit", client_id=client_id)
                return token

        log.debug(
            "auth_handler.fetching_token", token_url=token_url, client_id=client_id
        )

        data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scope:
            data["scope"] = scope

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(token_url, data=data)
            response.raise_for_status()
            token_data = response.json()

        access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 3600)
        expires_at = time.time() + expires_in

        _token_cache[cache_key] = (access_token, expires_at)
        log.debug("auth_handler.token_fetched", expires_in=expires_in)
        return access_token
