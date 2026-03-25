"""
Encryption utility for sensitive tenant fields (genai_api_key).

Two modes — detected automatically from settings:

  LOCAL / DEV (SECRET_ENCRYPTION_KEY set, KEY_VAULT_URL not set):
    encrypt() → Fernet AES encrypt → stores ciphertext in MongoDB
    decrypt() → Fernet AES decrypt → returns plaintext at runtime

  PRODUCTION / AKS (KEY_VAULT_URL set):
    encrypt() → stores secret NAME as-is (Key Vault secret must be pre-created)
    decrypt() → fetches secret VALUE from Azure Key Vault
                Managed Identity handles auth — no credentials needed

  PASSTHROUGH (neither set):
    encrypt() → stores value as-is (plaintext — dev only, no real secrets)
    decrypt() → returns value as-is

Usage:
    # Orchestrator — on tenant create/patch
    from app.core.encryption import encrypt_value
    encrypted = encrypt_value(plaintext_api_key)
    # store encrypted in MongoDB

    # Worker / Retrieval — on task start
    from app.core.encryption import decrypt_value
    api_key = await decrypt_value(stored_value)

Generate a Fernet key:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    Add to .env: SECRET_ENCRYPTION_KEY=<output>
"""

import structlog
from app.core.config import settings

log = structlog.get_logger(__name__)


def encrypt_value(plaintext: str) -> str:
    """
    Encrypt a sensitive value for storage in MongoDB.

    Local dev:  Fernet encrypt → returns ciphertext string
    Production: returns value as-is (Key Vault secret name)
    No key set: returns value as-is (plaintext — dev without real secrets)
    """
    if not plaintext:
        return plaintext

    if settings.KEY_VAULT_URL:
        # Production — value IS the Key Vault secret name, store as-is
        log.debug("encryption.keyvault_mode", action="passthrough")
        return plaintext

    if settings.SECRET_ENCRYPTION_KEY:
        from cryptography.fernet import Fernet

        f = Fernet(settings.SECRET_ENCRYPTION_KEY.encode())
        encrypted = f.encrypt(plaintext.encode()).decode()
        log.debug("encryption.fernet_encrypted")
        return encrypted

    # No encryption configured — store plaintext
    # Acceptable for local dev with test keys, not for production
    log.warning(
        "encryption.no_key_configured",
        message="Storing api_key without encryption. "
        "Set SECRET_ENCRYPTION_KEY in .env for local dev.",
    )
    return plaintext


async def decrypt_value(stored_value: str) -> str:
    """
    Decrypt a stored sensitive value at runtime.

    Local dev:  Fernet decrypt → returns plaintext
    Production: fetch from Azure Key Vault using Managed Identity
    No key set: returns value as-is
    """
    if not stored_value:
        return stored_value

    if settings.KEY_VAULT_URL:
        # Production — stored_value is the Key Vault secret NAME
        return await _fetch_from_keyvault(stored_value)

    if settings.SECRET_ENCRYPTION_KEY:
        from cryptography.fernet import Fernet, InvalidToken

        try:
            f = Fernet(settings.SECRET_ENCRYPTION_KEY.encode())
            return f.decrypt(stored_value.encode()).decode()
        except InvalidToken:
            # Value may be plaintext (stored before encryption was enabled)
            log.warning(
                "encryption.decrypt_failed",
                message="Could not decrypt — value may be plaintext. "
                "Re-save the api_config to encrypt it.",
            )
            return stored_value

    # No encryption — return as-is
    return stored_value


async def _fetch_from_keyvault(secret_name: str) -> str:
    """
    Fetch a secret from Azure Key Vault.
    Uses DefaultAzureCredential — works with Managed Identity on AKS,
    and with 'az login' for local dev against a real Key Vault.
    """
    try:
        from azure.keyvault.secrets import SecretClient
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        client = SecretClient(
            vault_url=settings.KEY_VAULT_URL,
            credential=credential,
        )
        # Run sync Key Vault call in thread pool (SDK is sync)
        import asyncio

        secret = await asyncio.to_thread(lambda: client.get_secret(secret_name).value)
        log.debug("encryption.keyvault_fetched", secret_name=secret_name)
        return secret
    except Exception as e:
        log.error(
            "encryption.keyvault_failed",
            secret_name=secret_name,
            error=str(e),
        )
        raise RuntimeError(
            f"Failed to fetch secret '{secret_name}' from Key Vault: {e}"
        ) from e


# ── Credential encryption ──────────────────────────────────────────────────────

# Fields that must be encrypted per connector type.
# Hardcoded because sensitivity is not tenant-configurable —
# a client_secret is always sensitive regardless of tenant preference.
SENSITIVE_CREDENTIAL_FIELDS: dict[str, set[str]] = {
    "sharepoint": {"client_secret"},
    "sql": {"connection_string", "password"},
    "mongodb": {"connection_string", "mongo_uri", "password"},
    "local": set(),
}


def encrypt_credentials(source_type: str, credentials: dict) -> dict:
    """
    Encrypt sensitive credential fields for a given connector type.
    Non-sensitive fields (tenant_id, client_id etc.) are left as-is.

    Called by orchestrator RepoService on create/patch.
    """
    if not credentials:
        return credentials

    sensitive = SENSITIVE_CREDENTIAL_FIELDS.get(source_type.lower(), set())
    if not sensitive:
        return credentials

    result = dict(credentials)
    for field in sensitive:
        if result.get(field):
            result[field] = encrypt_value(result[field])
    return result


async def decrypt_credentials(source_type: str, credentials: dict) -> dict:
    """
    Decrypt sensitive credential fields for a given connector type.
    Non-sensitive fields are returned as-is.

    Called by worker ConnectorFactory before building the connector.
    """
    if not credentials:
        return credentials

    sensitive = SENSITIVE_CREDENTIAL_FIELDS.get(source_type.lower(), set())
    if not sensitive:
        return credentials

    result = dict(credentials)
    for field in sensitive:
        if result.get(field):
            result[field] = await decrypt_value(result[field])
    return result
