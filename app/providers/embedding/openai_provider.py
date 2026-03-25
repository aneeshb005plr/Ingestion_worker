"""
OpenAI Embedding Provider — uses LangChain's OpenAIEmbeddings internally.

Responsibilities:
  - Split texts into batches of batch_size
  - Call OpenAI sequentially per batch (simple, predictable)
  - Return embeddings in original order

Concurrency is NOT managed here.
The worker holds a shared asyncio.Semaphore(embedding_concurrency) that
controls how many embed_documents() calls are in-flight across all
concurrent documents. This is the right place to enforce rate limits —
not inside the provider where it can't see the full picture.
"""

import structlog
from langchain_openai import OpenAIEmbeddings

from app.providers.embedding.base import BaseEmbeddingProvider

log = structlog.get_logger(__name__)


class OpenAIEmbeddingProvider(BaseEmbeddingProvider):

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        batch_size: int = 512,
        base_url: str | None = None,
    ) -> None:
        """
        Args:
            api_key:    OpenAI API key
            model:      Embedding model name
            batch_size: Texts per API call (default 512)
            base_url:   Azure OpenAI base URL (None = use OpenAI default)
        """
        kwargs = dict(
            api_key=api_key,
            model=model,
            chunk_size=batch_size,
        )
        if base_url:
            kwargs["base_url"] = base_url
        self._lc_embeddings = OpenAIEmbeddings(**kwargs)
        self._model = model
        self._batch_size = batch_size
        self._dimensions = 1536 if "small" in model else 3072
        log.info(
            "embedding.provider.initialised",
            model=model,
            dimensions=self._dimensions,
            batch_size=batch_size,
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return self._model

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """
        Embed texts in batches, sequentially.

        Concurrency across multiple documents is controlled by the caller
        (worker's shared embedding_semaphore) — not here.
        LangChain handles retries and rate limit backoff internally.
        """
        if not texts:
            return []

        log.debug(
            "embedding.started",
            total_texts=len(texts),
            batch_size=self._batch_size,
            model=self._model,
        )

        embeddings = await self._lc_embeddings.aembed_documents(texts)

        log.debug("embedding.completed", total_texts=len(texts))
        return embeddings

    def as_langchain_embeddings(self) -> OpenAIEmbeddings:
        """
        Returns the underlying LangChain Embeddings object.
        Used by MongoDBAtlasVectorStore which requires a LangChain
        Embeddings instance for its constructor.
        """
        return self._lc_embeddings
