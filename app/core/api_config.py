"""
API config resolver — resolves per-tenant GenAI provider settings.

Separation of concerns:
  ingestion_defaults  → HOW to ingest (embedding_model, chunk_size, concurrency)
  api_config          → WHERE and WHO (api_key, base_url, llm_model)

Priority chain for each field:
  1. tenant api_config        (per-tenant override)
  2. tenant ingestion_defaults (for embedding_model)
  3. global settings           (service-level env vars)
  4. hardcoded default         (fallback)

Used by:
  - Ingestion worker   → embedding provider construction per task
  - Retrieval service  → embedding + LLM client construction per request
                         (copy this file to retrieval service)

Example tenant document in MongoDB:
  {
    "ingestion_defaults": {
      "embedding_model": "text-embedding-3-small",
      "chunk_size": 1024,
      ...
    },
    "api_config": {
      "genai_api_key":  "sk-pwc-...",
      "genai_base_url": "https://genai-sharedservice-americas.pwcinternal.com",
      "llm_model":      "gpt-4.1-mini"
    }
  }
"""

from dataclasses import dataclass
from typing import Optional

from app.core.config import settings
from app.core.encryption import decrypt_value


@dataclass
class ResolvedApiConfig:
    """
    Fully resolved API configuration for a tenant.
    All fields guaranteed non-null after resolution.
    """

    api_key: str
    base_url: Optional[str]  # None = use OpenAI default endpoint
    embedding_model: str  # resolved from ingestion_defaults → global setting
    llm_model: str  # resolved from api_config → global setting

    @property
    def is_tenant_key(self) -> bool:
        """True if using a tenant-specific key (not the global service key)."""
        return self.api_key != settings.OPENAI_API_KEY

    @property
    def needs_custom_provider(self) -> bool:
        """
        True if a custom OpenAI provider must be built for this tenant.
        False = reuse the global startup provider.
        """
        return (
            self.is_tenant_key
            or self.base_url is not None
            or self.embedding_model != settings.EMBEDDING_MODEL
        )


async def resolve_api_config(
    tenant_api_config: dict | None,
    tenant_ingestion_defaults: dict | None = None,
) -> ResolvedApiConfig:
    """
    Resolve effective API config for a tenant.

    Async because genai_api_key may need to be:
      - Fernet decrypted  (local dev — SECRET_ENCRYPTION_KEY set)
      - Key Vault fetched (production — KEY_VAULT_URL set)
      - Returned as-is    (no encryption configured)

    Args:
        tenant_api_config:          The api_config dict from MongoDB tenant document.
                                    Can be None if tenant has no custom config.
        tenant_ingestion_defaults:  The ingestion_defaults dict from MongoDB.
                                    Used to resolve embedding_model.

    Returns:
        ResolvedApiConfig with all fields populated — never null.
    """
    cfg = tenant_api_config or {}
    defaults = tenant_ingestion_defaults or {}

    # Decrypt genai_api_key if present
    # decrypt_value handles Fernet, Key Vault, or passthrough automatically
    stored_key = cfg.get("genai_api_key")
    api_key = await decrypt_value(stored_key) if stored_key else settings.OPENAI_API_KEY

    return ResolvedApiConfig(
        api_key=api_key,
        base_url=cfg.get("genai_base_url") or None,
        embedding_model=(
            defaults.get("embedding_model")  # ingestion_defaults owns this
            or settings.EMBEDDING_MODEL
        ),
        llm_model=(
            cfg.get("llm_model")  # api_config owns this
            or getattr(settings, "LLM_MODEL", "gpt-4.1-mini")
        ),
    )
