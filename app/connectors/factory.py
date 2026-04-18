"""
ConnectorFactory — maps source_type strings to connector classes.

Adding a new connector (e.g. S3):
  1. Create app/connectors/s3.py with class S3Connector(BaseConnector)
  2. Add "s3": S3Connector to the registry below
  3. That's it — zero other changes needed
"""

import structlog

from app.connectors.base import BaseConnector
from app.connectors.local_connector import LocalConnector
from app.connectors.sharepoint_connector import SharePointConnector
from app.connectors.onedrive_connector import OneDriveConnector
from app.connectors.azure_blob_connector import AzureBlobConnector
from app.connectors.mongodb_connector import MongoDBConnector
from app.connectors.sql_connector import SQLConnector
from app.core.encryption import decrypt_credentials

log = structlog.get_logger(__name__)

# Registry: source_type string → connector class
_REGISTRY: dict[str, type[BaseConnector]] = {
    "local": LocalConnector,
    "sharepoint": SharePointConnector,
    "onedrive": OneDriveConnector,
    "azure_blob": AzureBlobConnector,
    "mongodb": MongoDBConnector,
    "sql": SQLConnector,
}

# Connectors that use delta_token for incremental scans
_DELTA_TOKEN_CONNECTORS = {"sharepoint", "onedrive"}

# Connectors that use last_run_at for incremental scans
_LAST_RUN_AT_CONNECTORS = {"sql", "mongodb", "azure_blob"}


async def get_connector(
    source_type: str,
    connector_config: dict,
    credentials: dict,
    delta_token: str | None = None,
    last_run_at=None,
) -> BaseConnector:
    """
    Instantiate the correct connector for a given source type.

    Async because sensitive credential fields (client_secret,
    connection_string etc.) are decrypted here before the connector
    is built — decryption may involve a Key Vault network call in prod.

    Args:
        source_type:      e.g. "local", "sharepoint", "mongodb", "sql"
        connector_config: repo's connector_config from MongoDB
                          (paths, filters, table names etc — NOT secrets)
        credentials:      encrypted credentials from MongoDB — decrypted here
        delta_token:      SharePoint delta sync token from previous run (optional)
        last_run_at:      datetime of last successful run (optional)

    Raises ValueError for unknown source types.
    """
    source_type_lower = source_type.lower()
    connector_class = _REGISTRY.get(source_type_lower)
    if connector_class is None:
        supported = list(_REGISTRY.keys())
        raise ValueError(
            f"Unknown source_type '{source_type}'. " f"Supported types: {supported}"
        )

    # Decrypt sensitive credential fields before passing to connector
    # e.g. sharepoint: client_secret, sql: connection_string
    decrypted_creds = await decrypt_credentials(source_type, credentials)

    log.info("connector.selected", source_type=source_type)

    # SharePoint needs delta_token for incremental scans
    if source_type_lower in _DELTA_TOKEN_CONNECTORS:
        return connector_class(
            config=connector_config,
            credentials=decrypted_creds,
            delta_token=delta_token,
        )
    # Azure Blob uses last_run_at for delta detection
    if source_type_lower == "azure_blob":
        return connector_class(
            config=connector_config,
            credentials=decrypted_creds,
            last_run_at=last_run_at,
        )

    # SQL and MongoDB use last_run_at
    if source_type_lower in {"sql", "mongodb"}:
        return connector_class(
            config=connector_config,
            credentials=decrypted_creds,
            last_run_at=last_run_at,
        )

    # Default (local connector — no delta support)
    return connector_class(
        config=connector_config,
        credentials=decrypted_creds,
    )

    # # SQL and MongoDB need last_run_at for delta fetch (WHERE updated_at > last_run_at)
    # if last_run_at is not None and hasattr(connector, "last_run_at"):
    #     connector.last_run_at = last_run_at

    # return connector
