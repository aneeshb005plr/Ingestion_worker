"""
MongoDB connection lifecycle for the Ingestion Worker.
Identical pattern to the orchestrator — one shared AsyncMongoClient.

Called from worker.py startup/shutdown hooks, not per-task.
"""

import structlog
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import settings
from app.core.exceptions import DatabaseConnectionError

log = structlog.get_logger(__name__)


class _MongoState:
    client: AsyncMongoClient | None = None


_state = _MongoState()


async def connect_to_mongo() -> None:
    log.info("mongodb.connecting")
    try:
        _state.client = AsyncMongoClient(
            settings.MONGO_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            tz_aware=True,
        )
        await _state.client.admin.command("ping")
        log.info("mongodb.connected", db=settings.MONGO_DB_NAME)
    except Exception as e:
        log.error("mongodb.connection_failed", error=str(e))
        raise DatabaseConnectionError(f"Cannot connect to MongoDB: {e}") from e


async def close_mongo_connection() -> None:
    if _state.client is not None:
        await _state.client.close()
        log.info("mongodb.disconnected")


def get_database() -> AsyncDatabase:
    if _state.client is None:
        raise DatabaseConnectionError(
            "MongoDB client not initialised. "
            "connect_to_mongo() must be called in worker startup."
        )
    return _state.client[settings.MONGO_DB_NAME]
