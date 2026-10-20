"""Small wrapper around the operating system's secure credential store.

OAuth access and refresh tokens are deliberately kept out of MongoDB, the run
history, the browser, and environment variables. On Windows, ``keyring`` uses
the current user's Windows Credential Manager.
"""

from __future__ import annotations

import json
from typing import Any

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


def is_available() -> bool:
    try:
        _keyring_module()
        return True
    except CredentialStoreError:
        return False


def get_credentials(provider: str) -> dict[str, Any] | None:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown connector: {provider}")
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


def save_credentials(provider: str, credentials: dict[str, Any]) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown connector: {provider}")
    keyring = _keyring_module()
    try:
        keyring.set_password(SERVICE_NAME, provider, json.dumps(credentials, separators=(",", ":")))
    except Exception as exc:
        raise CredentialStoreError("Could not save credentials to the OS credential store.") from exc


def delete_credentials(provider: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown connector: {provider}")
    keyring = _keyring_module()
    try:
        if keyring.get_password(SERVICE_NAME, provider) is not None:
            keyring.delete_password(SERVICE_NAME, provider)
    except Exception as exc:
        raise CredentialStoreError("Could not remove credentials from the OS credential store.") from exc
