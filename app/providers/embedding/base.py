"""
BaseEmbeddingProvider — abstract interface for all embedding providers.

Implementing a new provider (e.g. Azure OpenAI, local model):
  1. Create app/providers/embedding/azure_openai_provider.py
  2. Inherit from BaseEmbeddingProvider, implement embed_documents()
  3. Add it to the factory in ingestion_service.py

The provider handles batching internally — callers just pass all texts
and get all embeddings back. They don't need to know about batch sizes
or rate limits.
"""

from abc import ABC, abstractmethod


class BaseEmbeddingProvider(ABC):

    @abstractmethod
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a list of texts and return their vector representations.

        - Handles batching internally (splits into chunks of batch_size)
        - Handles rate limiting with retries (tenacity)
        - Returns embeddings in the same order as input texts

        Args:
            texts: list of text strings to embed

        Returns:
            list of float vectors, same length as input
        """
        pass

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Number of dimensions in the output vectors. e.g. 1536 for text-embedding-3-small"""
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Name of the embedding model. e.g. 'text-embedding-3-small'"""
        pass
