"""
ConnectorFactory — maps source_type strings to connector classes.

Adding a new connector (e.g. S3):
  1. Create app/connectors/s3_connector.py with class S3Connector(BaseConnector)
  2. Add "s3": S3Connector to the registry below
  3. That's it — zero other changes needed
"""

import structlog

from app.connectors.base import BaseConnector
from app.connectors.local_connector import LocalConnector
from app.connectors.sharepoint_connector import SharePointConnector
from app.connectors.mongodb_connector import MongoDBConnector
from app.connectors.sql_connector import SQLConnector

log = structlog.get_logger(__name__)

# Registry: source_type string → connector class
_REGISTRY: dict[str, type[BaseConnector]] = {
    "local": LocalConnector,
    "sharepoint": SharePointConnector,
    "mongodb": MongoDBConnector,
    "sql": SQLConnector,
}


def get_connector(
    source_type: str,
    connector_config: dict,
    credentials: dict,
    delta_token: str | None = None,
) -> BaseConnector:
    """
    Instantiate the correct connector for a given source type.

    Args:
        source_type:      e.g. "local", "sharepoint", "mongodb", "sql"
        connector_config: repo's connector_config from MongoDB
                          (paths, filters, table names etc — NOT secrets)
        credentials:      decrypted credentials from MongoDB
                          (passwords, API keys, connection strings etc)
        delta_token:      SharePoint delta sync token from previous run (optional)
                          Only used by SharePointConnector — others ignore it.

    Raises ValueError for unknown source types.
    """
    connector_class = _REGISTRY.get(source_type.lower())
    if connector_class is None:
        supported = list(_REGISTRY.keys())
        raise ValueError(
            f"Unknown source_type '{source_type}'. " f"Supported types: {supported}"
        )
    log.info("connector.selected", source_type=source_type)

    # SharePoint needs delta_token for incremental scans
    if source_type.lower() == "sharepoint":
        return connector_class(
            config=connector_config,
            credentials=credentials,
            delta_token=delta_token,
        )
    return connector_class(config=connector_config, credentials=credentials)
