"""OAuth connection lifecycle for the single-user console."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from typing import Any, Literal
from urllib.parse import urlencode

import requests

from app.config import settings
from app.services import credential_store
from app.services.credential_store import CredentialStoreError

Provider = Literal["github", "gmail"]
_STATE_TTL_SECONDS = 600
_pending: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()


class ConnectorError(RuntimeError):
    """A connector could not be started or authorized."""


def _provider_credentials(provider: Provider) -> tuple[str, str]:
    if provider == "github":
        client_id = settings.github_oauth_client_id
        client_secret = settings.github_oauth_client_secret
        env_names = "GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET"
    else:
        client_id = settings.google_client_id
        client_secret = settings.google_client_secret
        env_names = "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"
    if not client_id or not client_secret:
        raise ConnectorError(f"Configure {env_names} in backend/.env, then restart the backend.")
    return client_id, client_secret


def redirect_uri(provider: Provider) -> str:
    base = settings.backend_public_url.rstrip("/")
    return f"{base}/api/connectors/{provider}/callback"


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _remember_attempt(provider: Provider, state: str, verifier: str) -> None:
    now = time.time()
    with _pending_lock:
        expired = [key for key, item in _pending.items() if item["expires_at"] <= now]
        for key in expired:
            _pending.pop(key, None)
        _pending[state] = {
            "provider": provider,
            "verifier": verifier,
            "expires_at": now + _STATE_TTL_SECONDS,
        }


def _consume_attempt(provider: Provider, state: str) -> dict[str, Any]:
    if not state:
        raise ConnectorError(
            "OAuth state is missing. Start the connection from the console's Connect button; "
            "do not open the callback URL directly."
        )
    with _pending_lock:
        attempt = _pending.pop(state, None)
    if attempt is None or attempt["expires_at"] <= time.time():
        raise ConnectorError("OAuth state expired or was already used. Start the connection again.")
    if attempt["provider"] != provider:
        raise ConnectorError("OAuth provider mismatch. Start the connection again.")
    return attempt


def validate_callback_state(provider: Provider, state: str) -> None:
    """Consume and validate state when the provider returns an access denial."""
    _consume_attempt(provider, state)


def begin_connection(provider: Provider) -> dict[str, str]:
    client_id, _ = _provider_credentials(provider)
    if not credential_store.is_available():
        raise CredentialStoreError(
            "Enable Windows Credential Manager so connector tokens can be stored securely, then restart the backend."
        )

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    _remember_attempt(provider, state, verifier)
    common = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(provider),
        "response_type": "code",
        "state": state,
        "code_challenge": _code_challenge(verifier),
        "code_challenge_method": "S256",
    }

    if provider == "github":
        query = urlencode({**common, "scope": "repo read:user user:email offline_access"})
        authorization_url = f"https://github.com/login/oauth/authorize?{query}"
    else:
        query = urlencode(
            {
                **common,
                "scope": (
                    "https://www.googleapis.com/auth/gmail.readonly "
                    "https://www.googleapis.com/auth/gmail.send"
                ),
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
            }
        )
        authorization_url = f"https://accounts.google.com/o/oauth2/v2/auth?{query}"

    return {"authorization_url": authorization_url}


def _json_response(response: requests.Response, provider: Provider) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ConnectorError(f"{provider.title()} returned an unreadable OAuth response.") from exc
    if not isinstance(payload, dict):
        raise ConnectorError(f"{provider.title()} returned an unreadable OAuth response.")
    if response.status_code >= 400 or payload.get("error"):
        # OAuth providers return stable, non-secret error codes such as
        # ``invalid_grant`` and ``invalid_client``. Include that code in the
        # callback diagnostic so the console operator can correct the setup.
        error_code = payload.get("error")
        if isinstance(error_code, str) and error_code:
            raise ConnectorError(
                f"{provider.title()} authorization failed ({error_code}). "
                "Check the OAuth client and callback URL, then reconnect."
            )
        raise ConnectorError(f"{provider.title()} authorization failed. Check the OAuth app and callback URL.")
    if not payload.get("access_token"):
        raise ConnectorError(f"{provider.title()} did not return an access token.")
    return payload


def _token_expiry(value: Any, provider: Provider) -> float | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ConnectorError(f"{provider.title()} returned an invalid token lifetime.") from exc
    if seconds <= 0:
        raise ConnectorError(f"{provider.title()} returned an invalid token lifetime.")
    return time.time() + seconds


def _exchange_code(provider: Provider, code: str, verifier: str) -> dict[str, Any]:
    client_id, client_secret = _provider_credentials(provider)
    callback = redirect_uri(provider)
    if provider == "github":
        url = "https://github.com/login/oauth/access_token"
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": callback,
            "code_verifier": verifier,
        }
    else:
        url = "https://oauth2.googleapis.com/token"
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": callback,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
        }
    try:
        response = requests.post(
            url,
            data=data,
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise ConnectorError(f"Could not reach {provider.title()} to finish authorization.") from exc
    return _json_response(response, provider)


def _github_account(access_token: str) -> str:
    try:
        response = requests.get(
            "https://api.github.com/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ConnectorError("GitHub authorized the app, but the account could not be verified.") from exc
    if not isinstance(payload, dict):
        raise ConnectorError("GitHub authorized the app, but returned an unreadable account response.")
    account = payload.get("login")
    if not account:
        raise ConnectorError("GitHub did not return an account name.")
    return str(account)


def _gmail_account(access_token: str) -> str:
    try:
        response = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ConnectorError("Google authorized the app, but the Gmail account could not be verified.") from exc
    if not isinstance(payload, dict):
        raise ConnectorError("Google authorized the app, but returned an unreadable account response.")
    account = payload.get("emailAddress")
    if not account:
        raise ConnectorError("Gmail did not return an account address.")
    return str(account)


def finish_connection(provider: Provider, code: str, state: str) -> dict[str, str]:
    attempt = _consume_attempt(provider, state)
    if not code:
        raise ConnectorError("The provider returned no authorization code. Start the connection again.")
    tokens = _exchange_code(provider, code, attempt["verifier"])
    access_token = str(tokens["access_token"])
    account = _github_account(access_token) if provider == "github" else _gmail_account(access_token)
    refresh_token = tokens.get("refresh_token")
    if provider == "gmail" and not refresh_token:
        raise ConnectorError("Google did not return a refresh token. Reconnect and approve offline access.")

    expires_in = tokens.get("expires_in")
    credentials = {
        "access_token": access_token,
        "refresh_token": str(refresh_token) if refresh_token else None,
        "expires_at": _token_expiry(expires_in, provider),
        "refresh_expires_at": _token_expiry(tokens.get("refresh_token_expires_in"), provider),
        "account": account,
        "scope": tokens.get("scope"),
    }
    credential_store.save_credentials(provider, credentials)
    return {"provider": provider, "account": account}


def _revoke_remote(provider: Provider, credentials: dict[str, Any]) -> bool:
    token = str(credentials.get("refresh_token") or credentials.get("access_token") or "")
    if not token:
        return True
    try:
        if provider == "gmail":
            response = requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": token},
                timeout=15,
            )
            return response.status_code == 200
        if not settings.github_oauth_client_id or not settings.github_oauth_client_secret:
            return False
        response = requests.delete(
            f"https://api.github.com/applications/{settings.github_oauth_client_id}/grant",
            auth=(settings.github_oauth_client_id, settings.github_oauth_client_secret),
            json={"access_token": str(credentials.get("access_token") or token)},
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        return response.status_code in (204, 404)
    except requests.RequestException:
        return False


def disconnect(provider: Provider) -> bool:
    """Forget local tokens, and report whether remote revocation succeeded."""
    credentials = credential_store.get_credentials(provider)
    revoked = _revoke_remote(provider, credentials or {}) if credentials else True
    credential_store.delete_credentials(provider)
    return revoked


def connected_account(provider: Provider) -> str | None:
    try:
        credentials = credential_store.get_credentials(provider)
    except CredentialStoreError:
        return None
    account = credentials.get("account") if credentials else None
    return str(account) if account else None


def gmail_report_recipient() -> str | None:
    """Return only the connected Gmail identity as the final-report recipient."""
    address = connected_account("gmail")
    if address:
        return address
    if settings.gmail_configured and settings.gmail_sender_email != "you@example.com":
        return settings.gmail_sender_email.strip() or None
    return None
