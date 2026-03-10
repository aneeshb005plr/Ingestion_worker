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
from langchain_mongodb import MongoDBAtlasVectorSearch
from langchain_openai import OpenAIEmbeddings
from pymongo.asynchronous.database import AsyncDatabase

from app.providers.vectorstore.base import BaseVectorStore, VectorChunk

from app.core.config import settings

log = structlog.get_logger(__name__)

VECTOR_COLLECTION = "vector_store"
TEXT_KEY = "text"
EMEDDING_KEY = "embedding"


class MongoDBAtlasVectorStore(BaseVectorStore):

    def __init__(
        self,
        db: AsyncDatabase,
        index_name: str,
        lc_embeddings: OpenAIEmbeddings,
    ) -> None:
        """
        Args:
            db:             Async MongoDB database handle
            index_name:     Atlas Search index name for this tenant/repo
                            e.g. "vidx_tenant_docassist"
            lc_embeddings:  LangChain Embeddings object from our provider
                            obtained via provider.as_langchain_embeddings()
        """
        self._index_name = index_name
        self._async_collection = db[VECTOR_COLLECTION]  # for async delete

        # Synchronous MongoClient for LangChain — LangChain's MongoDB
        # integration uses sync pymongo internally (bulk_write etc.)
        sync_client = MongoClient(
            settings.MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=True
        )

        self._sync_collection = sync_client[settings.MONGO_DB_NAME][VECTOR_COLLECTION]

        # LangChain store — used only for similarity_search() in retrieval service
        # We do NOT use it for insertion (doesn't support pre-computed embeddings)
        self._store = MongoDBAtlasVectorSearch(
            collection=self._sync_collection,  # <- sync colection for langchain
            embedding=lc_embeddings,
            index_name=index_name,
            text_key=TEXT_KEY,  # field name for chunk text
            embedding_key=EMEDDING_KEY,  # field name for float vector
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
                EMEDDING_KEY: chunk.embedding,
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
        """
        Delete all chunks for a specific source document.
        Uses the raw collection directly — LangChain doesn't expose
        a clean delete-by-filter API, so we use pymongo here.
        """
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
        """
        Returns the underlying LangChain MongoDBAtlasVectorSearch instance.
        Used by the retrieval service for similarity_search() queries.
        This is the bridge — retrieval service imports this, not the full class.
        """
        return self._store
