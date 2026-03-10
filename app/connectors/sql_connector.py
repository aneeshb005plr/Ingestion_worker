"""
SQLConnector — reads rows from a SQL database table.

Supports content-hash based delta detection (no native delta API).

supports_native_deletes = False (default)
  → Worker detects deleted rows in Step 5 by comparing seen_source_ids
    against the ingested_documents registry.

Delta strategy:
  If delta_column is configured and last_run_at is available:
    → SELECT ... WHERE delta_column > last_run_at (incremental)
  Otherwise:
    → Full table scan, deduplicator skips unchanged rows by hash

Supports PostgreSQL (asyncpg) and MySQL (aiomysql) via SQLAlchemy async.

Config keys (connector_config):
  table           : str  — Table name e.g. "customers" or "dbo.invoices"
  primary_key     : str  — Column used as document identity e.g. "id"
  delta_column    : str  — Column for incremental fetch e.g. "updated_at" (optional)
  columns_include : list — Columns to include (empty = all columns)
  columns_exclude : list — Columns to exclude e.g. ["password_hash", "internal_flag"]
  column_descriptions : dict — Human-readable labels for columns (optional)
                                e.g. {"cust_id": "Customer ID", "amt": "Invoice Amount"}
  batch_size      : int  — rows per DB fetch (default: 500)

Credentials:
  connection_string : str — SQLAlchemy async URL
                            PostgreSQL: postgresql+asyncpg://user:pass@host/db
                            MySQL:      mysql+aiomysql://user:pass@host/db
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import AsyncGenerator

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncConnection

from app.connectors.base import BaseConnector, DeltaItem, RawDocument
from app.core.exceptions import ConnectorError

log = structlog.get_logger(__name__)


class SQLConnector(BaseConnector):

    # supports_native_deletes = False (inherited default)
    # Worker will compare seen_source_ids against registry after the run.

    def __init__(self, config: dict, credentials: dict) -> None:
        connection_string = credentials.get("connection_string", "")
        if not connection_string:
            raise ConnectorError("SQL credentials missing: connection_string required")

        self.table = config["table"]
        self.primary_key = config["primary_key"]
        self.delta_column = config.get("delta_column")
        self.columns_include: list[str] = config.get("columns_include", [])
        self.columns_exclude: set[str] = set(config.get("columns_exclude", []))
        self.column_descriptions: dict = config.get("column_descriptions", {})
        self.batch_size: int = config.get("batch_size", 500)

        # last_run_at injected by worker if available — enables delta fetch
        self.last_run_at: datetime | None = None

        # SQLAlchemy async engine
        self._engine = create_async_engine(
            connection_string,
            pool_pre_ping=True,
        )
        self._conn: AsyncConnection | None = None

    async def connect(self) -> None:
        """Open connection and verify DB is reachable."""
        try:
            self._conn = await self._engine.connect()
            await self._conn.execute(text("SELECT 1"))
            log.info("connector.sql.connected", table=self.table)
        except Exception as e:
            raise ConnectorError(f"SQL connection failed: {e}") from e

    async def fetch_documents(self) -> AsyncGenerator[DeltaItem, None]:
        """
        Yield DeltaItems for all matching rows in the table.

        Delta strategy:
          - If delta_column configured AND last_run_at available:
              adds WHERE delta_column > :last_run_at → incremental
          - Otherwise: full scan, deduplicator skips unchanged rows by hash
        """
        if self._conn is None:
            raise ConnectorError("SQL not connected — call connect() first")

        # Build SELECT clause
        if self.columns_include:
            # Always include primary key and delta column
            cols = set(self.columns_include)
            cols.add(self.primary_key)
            if self.delta_column:
                cols.add(self.delta_column)
            select_clause = ", ".join(cols)
        else:
            select_clause = "*"

        # Build WHERE clause
        where_parts = []
        params: dict = {}

        if self.delta_column and self.last_run_at:
            where_parts.append(f"{self.delta_column} > :last_run_at")
            params["last_run_at"] = self.last_run_at
            log.info(
                "connector.sql.delta_fetch",
                table=self.table,
                delta_column=self.delta_column,
                since=self.last_run_at.isoformat(),
            )
        else:
            log.info("connector.sql.full_scan", table=self.table)

        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        query = text(f"SELECT {select_clause} FROM {self.table} {where_clause}")

        count = 0
        offset = 0

        while True:
            # Paginate with LIMIT/OFFSET to avoid loading entire table into memory
            paginated = text(
                f"SELECT {select_clause} FROM {self.table} {where_clause} "
                f"LIMIT :limit OFFSET :offset"
            )
            result = await self._conn.execute(
                paginated,
                {**params, "limit": self.batch_size, "offset": offset},
            )
            rows = result.fetchall()

            if not rows:
                break

            for row in rows:
                try:
                    row_dict = dict(row._mapping)

                    # Apply column exclusions
                    row_dict = {
                        k: v
                        for k, v in row_dict.items()
                        if k not in self.columns_exclude
                    }

                    # Build stable source_id from primary key
                    pk_value = str(row_dict.get(self.primary_key, ""))
                    source_id = f"{self.table}/{pk_value}"

                    # Content hash — serialise to JSON for stable comparison
                    row_json = json.dumps(row_dict, default=str, sort_keys=True)
                    content_hash = hashlib.sha256(row_json.encode()).hexdigest()

                    # last_modified from delta_column if available
                    last_modified: datetime | None = None
                    if self.delta_column and self.delta_column in row_dict:
                        raw_ts = row_dict[self.delta_column]
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
                            raw_data=row_dict,
                            structured_source_type="sql",
                            source_id=source_id,
                            file_name=f"{self.table}/{pk_value}",
                            full_path=f"/{self.table}/{pk_value}",
                            source_url=f"sql://{self.table}/{pk_value}",
                            content_hash=content_hash,
                            last_modified_at_source=last_modified,
                            extra_metadata={
                                "table": self.table,
                                "primary_key": self.primary_key,
                                "pk_value": pk_value,
                                "column_descriptions": self.column_descriptions,
                            },
                        ),
                    )
                    count += 1

                except Exception as e:
                    log.warning(
                        "connector.sql.row_error",
                        table=self.table,
                        error=str(e),
                    )
                    continue

            offset += self.batch_size

        log.info(
            "connector.sql.fetch_complete",
            table=self.table,
            rows_yielded=count,
        )

    async def disconnect(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        await self._engine.dispose()
        log.info("connector.sql.disconnected")
