"""Store connector OAuth credentials per Google account.

Local, unscoped callers retain the OS keyring behavior. Authenticated users use
MongoDB with an application-secret-derived Fernet key so hosted instances can
persist credentials without sharing them across accounts.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from app.config import settings

SERVICE_NAME = "ai-dev-team-console"
PROVIDERS = frozenset({"github", "gmail"})


class CredentialStoreError(RuntimeError):
    """The operating system credential store could not be used safely."""


def _keyring_module() -> Any:
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise CredentialStoreError("Install the backend requirements to enable secure connector storage.") from exc

    try:
        backend = keyring.get_keyring()
        if float(getattr(backend, "priority", 0)) <= 0:
            raise CredentialStoreError(
                "No secure OS credential store is available. Enable Windows Credential Manager and restart the backend."
            )
    except CredentialStoreError:
        raise
    except Exception as exc:
        raise CredentialStoreError("The OS credential store is unavailable.") from exc
    return keyring


def _fernet() -> Any:
    secret = settings.auth_session_secret or settings.google_client_secret
    if not secret:
        raise CredentialStoreError("Set AUTH_SESSION_SECRET to enable encrypted account connections.")
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - dependency installation
        raise CredentialStoreError("Install the backend requirements to enable encrypted account storage.") from exc
    key = base64.urlsafe_b64encode(hashlib.sha256(f"ai-dev-team-connectors:{secret}".encode()).digest())
    return Fernet(key)


def is_available(user_id: str | None = None) -> bool:
    if user_id is not None:
        try:
            _fernet()
            from app.db import database_status

            return database_status().get("status") == "ok"
        except CredentialStoreError:
            return False
    try:
        _keyring_module()
        return True
    except CredentialStoreError:
        return False


def _read_user_credentials(provider: str, user_id: str) -> dict[str, Any] | None:
    from cryptography.fernet import InvalidToken
    from app.db import get_user_credential

    try:
        encrypted = get_user_credential(user_id, provider)
        if not encrypted:
            return None
        payload = json.loads(_fernet().decrypt(encrypted.encode("ascii")))
    except InvalidToken as exc:
        raise CredentialStoreError("Stored account credentials could not be decrypted. Reconnect this account.") from exc
    except CredentialStoreError:
        raise
    except Exception as exc:
        raise CredentialStoreError("Could not read the encrypted account credentials from MongoDB.") from exc
    if not isinstance(payload, dict):
        raise CredentialStoreError("Stored connector credentials are unreadable. Disconnect and reconnect the account.")
    return payload


def get_credentials(provider: str, user_id: str | None = None) -> dict[str, Any] | None:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown connector: {provider}")
    if user_id is not None:
        return _read_user_credentials(provider, user_id)
    keyring = _keyring_module()
    try:
        value = keyring.get_password(SERVICE_NAME, provider)
    except Exception as exc:
        raise CredentialStoreError("Could not read the OS credential store.") from exc
    if not value:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise CredentialStoreError("Stored connector credentials are unreadable. Disconnect and reconnect the account.") from exc
    if not isinstance(payload, dict):
        raise CredentialStoreError("Stored connector credentials are unreadable. Disconnect and reconnect the account.")
    return payload


def save_credentials(provider: str, credentials: dict[str, Any], user_id: str | None = None) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown connector: {provider}")
    if user_id is not None:
        from app.db import save_user_credential

        try:
            encrypted = _fernet().encrypt(json.dumps(credentials, separators=(",", ":")).encode()).decode("ascii")
            save_user_credential(user_id, provider, encrypted)
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreError("Could not save encrypted account credentials to MongoDB.") from exc
        return
    keyring = _keyring_module()
    try:
        keyring.set_password(SERVICE_NAME, provider, json.dumps(credentials, separators=(",", ":")))
    except Exception as exc:
        raise CredentialStoreError("Could not save credentials to the OS credential store.") from exc


def delete_credentials(provider: str, user_id: str | None = None) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown connector: {provider}")
    if user_id is not None:
        from app.db import delete_user_credential

        try:
            delete_user_credential(user_id, provider)
        except Exception as exc:
            raise CredentialStoreError("Could not remove encrypted account credentials from MongoDB.") from exc
        return
    keyring = _keyring_module()
    try:
        if keyring.get_password(SERVICE_NAME, provider) is not None:
            keyring.delete_password(SERVICE_NAME, provider)
    except Exception as exc:
        raise CredentialStoreError("Could not remove credentials from the OS credential store.") from exc


