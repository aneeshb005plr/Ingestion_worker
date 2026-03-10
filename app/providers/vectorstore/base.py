"""
BaseVectorStore — abstract interface for all vector store providers.

Implementing a new provider (e.g. Pinecone, Weaviate):
  1. Create app/providers/vectorstore/pinecone.py
  2. Inherit from BaseVectorStore, implement add_documents() and delete_by_source()
  3. Update worker.py to select it

The key insight: ingestion_service.py never imports MongoDBAtlasVectorStore directly.
It only ever calls self.vector_store.add_documents(...).
Swapping the provider = zero changes to ingestion_service.py.
"""

from abc import ABC, abstractmethod
from typing import Any
from pydantic import BaseModel, Field


class VectorChunk(BaseModel):
    """
    A single chunk ready to be stored in the vector store.

    Pydantic validates:
      - text is a string (not bytes, not None)
      - embedding is a list of floats (not ints, not strings)
      - metadata is a dict

    This matters because a wrong embedding type would silently corrupt
    Atlas vector search results — better to catch it here at construction.
    """

    text: str
    embedding: list[float]
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseVectorStore(ABC):

    @abstractmethod
    async def add_documents(
        self,
        chunks: list[VectorChunk],
        tenant_id: str,
        repo_id: str,
    ) -> int:
        """
        Store a list of embedded chunks.
        Returns the number of chunks stored.
        """
        pass

    @abstractmethod
    async def delete_by_source(
        self,
        source_id: str,
        tenant_id: str,
        repo_id: str,
    ) -> int:
        """
        Delete all chunks for a specific source document.
        Called before re-embedding an updated document.
        Returns the number of chunks deleted.
        """
        pass
