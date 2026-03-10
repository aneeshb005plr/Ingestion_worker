"""
RepoConfigRepository — reads repository config and credentials from MongoDB.

This is the FIRST thing the worker does in every task:
  1. Load the repo config (connector type, paths, schedule, vector config)
  2. Load the credentials (SharePoint client_secret, etc.)

Security note:
  Credentials are loaded here, held in memory for the task duration only.
  They are NEVER logged, never returned to the queue, never stored elsewhere.
"""

import structlog
from pymongo.asynchronous.database import AsyncDatabase

from app.repositories.base import BaseRepository
from app.core.exceptions import RepoConfigNotFoundError

log = structlog.get_logger(__name__)


class RepoConfigRepository(BaseRepository):

    def __init__(self, db: AsyncDatabase) -> None:
        super().__init__(db)
        self.repos = db["source_repositories"]
        self.tenants = db["tenants"]
        self.credentials = db["credentials"]

    async def get_repo(self, repo_id: str) -> dict:
        """
        Load full repository configuration.
        Raises RepoConfigNotFoundError if not found or inactive.
        """
        doc = await self.repos.find_one({"_id": repo_id, "is_active": True})
        if not doc:
            raise RepoConfigNotFoundError(
                f"Repository '{repo_id}' not found or is inactive."
            )
        return doc

    async def get_tenant(self, tenant_id: str) -> dict:
        """Load tenant configuration (defaults, metadata schema, etc.)"""
        doc = await self.tenants.find_one({"_id": tenant_id})
        if not doc:
            raise RepoConfigNotFoundError(f"Tenant '{tenant_id}' not found.")
        return doc

    async def get_credentials(self, repo_id: str) -> dict:
        """
        Load credentials for a repo.
        Returns the raw credentials dict — worker keeps this in memory only.
        Phase 1: stored as plaintext. Phase 8: will be encrypted.
        """
        doc = await self.credentials.find_one({"repo_id": repo_id})
        if not doc:
            # Not all repos have credentials (e.g. local filesystem)
            return {}
        return doc.get("data", {})

    async def save_delta_token(self, repo_id: str, delta_token: str) -> None:
        """
        Save the SharePoint delta sync token after a successful run.
        Next run will use this token to fetch only changed files.
        """
        await self.repos.update_one(
            {"_id": repo_id}, {"$set": {"state.delta_token": delta_token}}
        )
        log.info("repo.delta_token_saved", repo_id=repo_id)

    async def update_last_ingested(self, repo_id: str, timestamp) -> None:
        """Update the last successful ingestion timestamp."""
        await self.repos.update_one(
            {"_id": repo_id}, {"$set": {"state.last_successful_run_at": timestamp}}
        )
