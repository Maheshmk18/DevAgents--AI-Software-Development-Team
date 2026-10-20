"""A non-secret view of configured external connections."""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.services import credential_store
from app.services.credential_store import CredentialStoreError


def _tone(configured: bool, short_lived: bool = False) -> str:
    if not configured:
        return "off"
    return "warn" if short_lived else "ok"


def _stored_credentials(provider: str) -> dict[str, Any] | None:
    try:
        return credential_store.get_credentials(provider)
    except CredentialStoreError:
        return None


def _github(storage_available: bool) -> dict[str, Any]:
    from app.mcp.tool_registry import default_repo_parts
    from app.services.connector_service import redirect_uri

    owner, repo = default_repo_parts()
    credentials = _stored_credentials("github") or {}
    oauth_connected = bool(credentials.get("access_token"))
    connected = oauth_connected or bool(settings.github_token)
    client_ready = bool(settings.github_oauth_client_id and settings.github_oauth_client_secret)
    return {
        "connected": connected,
        "tone": _tone(connected),
        "account": credentials.get("account"),
        "source": "oauth" if oauth_connected else "environment" if settings.github_token else None,
        "connect_enabled": client_ready and storage_available,
        "repository_selection_enabled": connected and storage_available,
        "callback_url": redirect_uri("github"),
        "setup_hint": (
            "Set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET in backend/.env."
            if not client_ready
            else "Enable Windows Credential Manager to store OAuth tokens securely."
            if not storage_available
            else None
        ),
        "owner": owner or None,
        "repo": repo or None,
        "slug": f"{owner}/{repo}" if owner and repo else None,
        "where": (
            "Connect in the console; the OAuth app credentials live in backend/.env."
            if oauth_connected
            else "GitHub token is configured in backend/.env as GITHUB_TOKEN."
            if settings.github_token
            else "Connect GitHub in the console or configure GITHUB_TOKEN in backend/.env."
        ),
    }


def _gmail(storage_available: bool) -> dict[str, Any]:
    from app.services.connector_service import redirect_uri

    credentials = _stored_credentials("gmail") or {}
    sender = settings.gmail_sender_email
    if sender == "you@example.com":
        sender = ""
    account = credentials.get("account") or sender or None
    oauth_connected = bool(credentials.get("access_token"))
    configured = oauth_connected or settings.gmail_configured
    mode = "oauth" if oauth_connected else settings.gmail_mode
    short_lived = mode == "access_token" and not oauth_connected
    client_ready = bool(settings.google_client_id and settings.google_client_secret)
    return {
        "connected": configured,
        "tone": _tone(configured, short_lived),
        "mode": mode,
        "sender": account,
        "source": "oauth" if oauth_connected else "environment" if settings.gmail_configured else None,
        "connect_enabled": client_ready and storage_available,
        "callback_url": redirect_uri("gmail"),
        "setup_hint": (
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET for a Google Web application OAuth client in backend/.env."
            if not client_ready
            else "Enable Windows Credential Manager to store OAuth tokens securely."
            if not storage_available
            else None
        ),
        "note": (
            "Short-lived access token: it expires about an hour after it was issued. "
            "Use GMAIL_REFRESH_TOKEN to keep sending working."
            if short_lived
            else None
        ),
        "where": "Connect in the console; the OAuth app credentials live in backend/.env.",
    }


def connection_status() -> dict[str, Any]:
    from app.db import database_status
    from app.services.llm_service import llm_service

    storage_available = credential_store.is_available()
    db = database_status()
    db_ok = db.get("status") == "ok"
    llm_status = llm_service.status()
    groq_ready = llm_status["configured"]
    groq_healthy = (
        groq_ready
        and llm_status["last_provider"] == "groq"
        and llm_status["last_error"] is None
    )
    return {
        "github": _github(storage_available),
        "gmail": _gmail(storage_available),
        "mongo": {
            "connected": db_ok,
            "tone": _tone(db_ok),
            "database": settings.mongo_db,
            "where": "backend/.env → MONGO_URI",
        },
        "llm": {
            "connected": groq_healthy,
            "tone": "ok" if groq_healthy else "warn" if groq_ready else "off",
            "provider": "groq",
            "model": settings.groq_model,
            "where": "backend/.env → GROQ_API_KEY, GROQ_MODEL",
        },
        "secure_storage_available": storage_available,
        "status": llm_status,
    }
