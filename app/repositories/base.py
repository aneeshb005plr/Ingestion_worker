"""Base repository — identical to orchestrator pattern."""

from abc import ABC
from pymongo.asynchronous.database import AsyncDatabase


class BaseRepository(ABC):
    def __init__(self, db: AsyncDatabase) -> None:
        self.db = db
