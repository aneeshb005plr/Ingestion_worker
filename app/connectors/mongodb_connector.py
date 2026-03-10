"""
MongoDBConnector — reads documents from a MongoDB collection.

Supports content-hash based delta detection (no native delta API).

supports_native_deletes = False (default)
  → Worker detects deletions in Step 5 by comparing seen_source_ids
    against the ingested_documents registry.

Delta strategy:
  If delta_field is configured and last_run_at is available:
    → fetch only documents where delta_field > last_run_at (incremental)
  Otherwise:
    → full scan with hash comparison (deduplicator handles skipping unchanged)

Config keys (connector_config):
  database        : str  — MongoDB database name
  collection      : str  — Collection name
  id_field        : str  — Field to use as document identity (default: "_id")
  delta_field     : str  — Field for incremental fetch e.g. "updated_at" (optional)
  filter_query    : dict — Extra MongoDB filter e.g. {"status": "active"} (optional)
  field_descriptions : dict — Human-readable labels for fields (optional)
                              e.g. {"cust_id": "Customer ID", "amt": "Invoice Amount"}
  skip_fields     : list — Fields to exclude from Markdown output (optional)
                           e.g. ["__v", "internal_flag"]

Credentials:
  connection_string : str — mongodb+srv://user:pass@cluster.mongodb.net
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import AsyncGenerator

import structlog
from pymongo import AsyncMongoClient

from app.connectors.base import BaseConnector, DeltaItem, RawDocument
from app.core.exceptions import ConnectorError

log = structlog.get_logger(__name__)


class MongoDBConnector(BaseConnector):

    # supports_native_deletes = False (inherited default)
    # Worker will compare seen_source_ids against registry after the run.

    def __init__(self, config: dict, credentials: dict) -> None:
        self._connection_string = credentials.get("connection_string", "")
        if not self._connection_string:
            raise ConnectorError(
                "MongoDB credentials missing: connection_string required"
            )

        self.database = config["database"]
        self.collection = config["collection"]
        self.id_field = config.get("id_field", "_id")
        self.delta_field = config.get("delta_field")  # e.g. "updated_at"
        self.filter_query: dict = config.get("filter_query", {})
        self.field_descriptions: dict = config.get("field_descriptions", {})
        self.skip_fields: set[str] = set(config.get("skip_fields", ["__v"]))

        # Populated in connect()
        self._client: AsyncMongoClient | None = None

        # last_run_at is injected by the worker if available — enables delta fetch
        self.last_run_at: datetime | None = None

    async def connect(self) -> None:
        """Connect and verify MongoDB is reachable."""
        try:
            self._client = AsyncMongoClient(
                self._connection_string,
                serverSelectionTimeoutMS=5000,
                tz_aware=True,
            )
            await self._client.admin.command("ping")
            log.info(
                "connector.mongodb.connected",
                database=self.database,
                collection=self.collection,
            )
        except Exception as e:
            raise ConnectorError(f"MongoDB connection failed: {e}") from e

    async def fetch_documents(self) -> AsyncGenerator[DeltaItem, None]:
        """
        Yield DeltaItems for all matching documents in the collection.

        Delta strategy:
          - If delta_field configured AND last_run_at available:
              query adds {delta_field: {$gt: last_run_at}} → incremental fetch
          - Otherwise: full scan, deduplicator skips unchanged docs by hash
        """
        if self._client is None:
            raise ConnectorError("MongoDB not connected — call connect() first")

        db = self._client[self.database]
        collection = db[self.collection]

        # Build query — start with any custom filter
        query = dict(self.filter_query)

        # Add delta filter if configured
        if self.delta_field and self.last_run_at:
            query[self.delta_field] = {"$gt": self.last_run_at}
            log.info(
                "connector.mongodb.delta_fetch",
                collection=self.collection,
                delta_field=self.delta_field,
                since=self.last_run_at.isoformat(),
            )
        else:
            log.info(
                "connector.mongodb.full_scan",
                collection=self.collection,
            )

        count = 0
        async for doc in collection.find(query):
            try:
                # Build stable source_id from id_field value
                doc_id = str(doc.get(self.id_field, doc.get("_id", "")))
                source_id = f"{self.database}/{self.collection}/{doc_id}"

                # Remove skip_fields and convert _id to string for serialisation
                clean_doc = {
                    k: (str(v) if k == "_id" else v)
                    for k, v in doc.items()
                    if k not in self.skip_fields
                }

                # Content hash — stable representation for dedup
                doc_json = json.dumps(clean_doc, default=str, sort_keys=True)
                content_hash = hashlib.sha256(doc_json.encode()).hexdigest()

                # last_modified from delta_field if available
                last_modified: datetime | None = None
                if self.delta_field and self.delta_field in doc:
                    raw_ts = doc[self.delta_field]
                    if isinstance(raw_ts, datetime):
                        last_modified = (
                            raw_ts
                            if raw_ts.tzinfo
                            else raw_ts.replace(tzinfo=timezone.utc)
                        )

                yield DeltaItem(
                    type="upsert",
                    source_id=source_id,
                    raw_doc=RawDocument(
                        content_type="structured",
                        raw_data=clean_doc,
                        structured_source_type="mongodb",
                        source_id=source_id,
                        file_name=f"{self.collection}/{doc_id}",
                        full_path=f"/{self.database}/{self.collection}/{doc_id}",
                        source_url=f"mongodb://{self.database}/{self.collection}/{doc_id}",
                        content_hash=content_hash,
                        last_modified_at_source=last_modified,
                        extra_metadata={
                            "database": self.database,
                            "collection": self.collection,
                            "document_id": doc_id,
                            "field_descriptions": self.field_descriptions,
                        },
                    ),
                )
                count += 1

            except Exception as e:
                log.warning(
                    "connector.mongodb.document_error",
                    collection=self.collection,
                    error=str(e),
                )
                continue

        log.info(
            "connector.mongodb.fetch_complete",
            collection=self.collection,
            documents_yielded=count,
        )

    async def disconnect(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        log.info("connector.mongodb.disconnected")
