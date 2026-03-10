"""
OpenAI Embedding Provider — uses LangChain's OpenAIEmbeddings internally.

Why LangChain here:
  - langchain-openai's OpenAIEmbeddings handles batching, retries, and async
    correctly out of the box and is actively maintained
  - It implements the LangChain Embeddings interface which MongoDBAtlasVectorSearch
    expects — no adapter needed
  - Our BaseEmbeddingProvider ABC wraps it, so the rest of the system
    never imports LangChain directly — we keep our abstraction layer intact

What our wrapper adds on top:
  - Exposes our own BaseEmbeddingProvider interface (dimensions, model_name)
  - Provides the LangChain Embeddings object via .as_langchain_embeddings()
    so MongoDBAtlasVectorStore can use it directly
  - Single place to configure the model for the whole system
"""

import structlog
from langchain_openai import OpenAIEmbeddings

from app.providers.embedding.base import BaseEmbeddingProvider

log = structlog.get_logger(__name__)


class OpenAIEmbeddingProvider(BaseEmbeddingProvider):

    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        # LangChain's OpenAIEmbeddings — handles batching + async internally
        self._lc_embeddings = OpenAIEmbeddings(
            openai_api_key=api_key,
            model=model,
            # chunk_size controls how many texts per API request
            # 512 is safe for text-embedding-3-small
            chunk_size=512,
        )
        self._model = model
        # text-embedding-3-small → 1536 dims, text-embedding-3-large → 3072 dims
        self._dimensions = 1536 if "small" in model else 3072
        log.info(
            "embedding.provider.initialised", model=model, dimensions=self._dimensions
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return self._model

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a list of texts using LangChain's OpenAIEmbeddings.
        LangChain handles batching and rate limit retries internally.
        """
        if not texts:
            return []
        log.info("embedding.started", total_texts=len(texts), model=self._model)
        embeddings = await self._lc_embeddings.aembed_documents(texts)
        log.info("embedding.completed", total_texts=len(texts))
        return embeddings

    def as_langchain_embeddings(self) -> OpenAIEmbeddings:
        """
        Returns the underlying LangChain Embeddings object.
        Used by MongoDBAtlasVectorStore which requires a LangChain
        Embeddings instance for its constructor.

        This is the bridge between our abstraction and LangChain's internals.
        Nothing else should call this method.
        """
        return self._lc_embeddings
