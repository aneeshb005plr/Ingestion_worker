"""
MongoDB Atlas Vector Store.

Architecture decision:
  LangChain's MongoDBAtlasVectorSearch does NOT support pre-computed embeddings
  for insertion — it always re-embeds internally. Since we already embed via our
  OpenAIEmbeddingProvider (with proper batching), re-embedding would double our
  OpenAI costs and latency.

  Solution — split responsibilities:
    INSERT:  Direct pymongo insert_many() with our pre-computed embeddings
             Uses SYNCHRONOUS MongoClient (required by LangChain schema compatibility)
    DELETE:  Direct async pymongo delete_many()
    SEARCH:  LangChain MongoDBAtlasVectorSearch.similarity_search() (Phase retrieval)
             Instantiated with sync collection — LangChain handles $vectorSearch query format

  Document schema written by us (must match what LangChain reads for search):
    {
      "text":      "chunk content",       ← text_key
      "embedding": [0.123, ...],           ← embedding_key
      ...all metadata fields at top level  ← LangChain flattens metadata on read
    }
"""

import asyncio
import structlog
from bson import ObjectId
from pymongo import MongoClient  # sync — for LangChain compat
from pymongo.asynchronous.database import AsyncDatabase
from langchain_mongodb import MongoDBAtlasVectorSearch
from langchain_openai import OpenAIEmbeddings

from app.providers.vectorstore.base import BaseVectorStore, VectorChunk
from app.core.config import settings

log = structlog.get_logger(__name__)

TEXT_KEY = "text"
EMBEDDING_KEY = "embedding"

# Default collection — used when index_mode="shared_tenant"
DEFAULT_VECTOR_COLLECTION = "vector_store"


def resolve_collection_name(vector_config: dict) -> str:
    """
    Resolve the MongoDB collection name from vector_config.

    Three-tier routing:
      shared_tenant    → "vector_store"              (default)
      dedicated_tenant → "vector_store_{tenant_id}"
      dedicated_repo   → "vector_store_{repo_id}"

    collection_name is pre-resolved at repo creation time
    by repo_service.py and stored in vector_config.
    We just read it here — no re-computation needed.
    """
    return vector_config.get("collection_name") or DEFAULT_VECTOR_COLLECTION


class MongoDBAtlasVectorStore(BaseVectorStore):

    def __init__(
        self,
        db: AsyncDatabase,
        index_name: str,
        lc_embeddings: OpenAIEmbeddings,
        collection_name: str = DEFAULT_VECTOR_COLLECTION,
    ) -> None:
        self._index_name = index_name
        self._collection_name = collection_name
        self._async_collection = db[collection_name]  # for async delete

        # Sync MongoClient — LangChain's MongoDBAtlasVectorSearch requires
        # a synchronous pymongo collection
        self._sync_client = MongoClient(
            settings.MONGO_URI,
            serverSelectionTimeoutMS=5000,
            tz_aware=True,
        )
        self._sync_collection = self._sync_client[settings.MONGO_DB_NAME][
            collection_name
        ]

        # LangChain store — used only for similarity_search() in retrieval service
        # We do NOT use it for insertion (doesn't support pre-computed embeddings)
        self._store = MongoDBAtlasVectorSearch(
            collection=self._sync_collection,
            embedding=lc_embeddings,
            index_name=index_name,
            text_key=TEXT_KEY,
            embedding_key=EMBEDDING_KEY,
            relevance_score_fn="cosine",
        )

        log.info("vectorstore.initialised", index=index_name)

    async def add_documents(
        self,
        chunks: list[VectorChunk],
        tenant_id: str,
        repo_id: str,
    ) -> int:
        """
        Insert pre-computed embeddings directly via insert_many.
        Schema matches what LangChain expects for similarity_search() later.
        Runs sync insert_many in thread pool — doesn't block event loop.
        """
        if not chunks:
            return 0

        docs = [
            {
                "_id": ObjectId(),
                TEXT_KEY: chunk.text,
                EMBEDDING_KEY: chunk.embedding,
                # Flatten metadata to top level — LangChain reads it this way
                **chunk.metadata,
                "tenant_id": tenant_id,
                "repo_id": repo_id,
            }
            for chunk in chunks
        ]

        # Run sync insert_many in thread pool
        def _insert():
            self._sync_collection.insert_many(docs)

        await asyncio.to_thread(_insert)

        log.info(
            "vectorstore.added",
            chunks=len(chunks),
            tenant_id=tenant_id,
            repo_id=repo_id,
            index=self._index_name,
        )
        return len(chunks)

    async def delete_by_source(
        self,
        source_id: str,
        tenant_id: str,
        repo_id: str,
    ) -> int:
        result = await self._async_collection.delete_many(
            {
                "source_id": source_id,
                "tenant_id": tenant_id,
                "repo_id": repo_id,
            }
        )
        count = result.deleted_count
        if count > 0:
            log.info(
                "vectorstore.deleted",
                source_id=source_id,
                chunks_deleted=count,
                tenant_id=tenant_id,
            )
        return count

    def as_langchain_store(self) -> MongoDBAtlasVectorSearch:
        """Returns LangChain store for retrieval service similarity_search()."""
        return self._store
