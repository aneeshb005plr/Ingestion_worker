from pydantic import RedisDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Worker configuration. Reads from environment variables / .env file.

    The worker needs everything the orchestrator needs PLUS:
    - OPENAI_API_KEY  — for embedding + vision (image descriptions)

    Anything that can change per-tenant (model name, chunk size, concurrency)
    is stored in MongoDB per-tenant config, NOT here.
    These settings are infrastructure-level only.
    """

    # ── App ───────────────────────────────────────────────────────────────────
    PROJECT_NAME: str = "Ingestion Worker"
    VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"

    # ── MongoDB (Atlas) ────────────────────────────────────────────────────────
    MONGO_URI: str
    MONGO_DB_NAME: str = "vector_platform"

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URI: RedisDsn = "redis://localhost:6379"  # type: ignore[assignment]
    QUEUE_NAME: str = "ingestion_jobs"

    # ── OpenAI ────────────────────────────────────────────────────────────────
    OPENAI_API_KEY: str
    EMBEDDING_MODEL: str = "text-embedding-3-small"  # default, overridable per tenant

    # ── Computed: parsed from REDIS_URI, not set manually ─────────────────────
    @computed_field  # type: ignore[misc]
    @property
    def REDIS_HOST(self) -> str:
        return str(self.REDIS_URI.host)

    @computed_field  # type: ignore[misc]
    @property
    def REDIS_PORT(self) -> int:
        return self.REDIS_URI.port or 6379

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()  # type: ignore[call-arg]
