"""Google OpenID Connect sign-in and short-lived app sessions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import requests

from app.config import settings

SESSION_COOKIE = "ai_dev_team_session"
OAUTH_STATE_COOKIE = "ai_google_oauth_state"
OAUTH_VERIFIER_COOKIE = "ai_google_oauth_verifier"
SESSION_TTL_SECONDS = 8 * 60 * 60
OAUTH_TTL_SECONDS = 10 * 60


class GoogleAuthError(RuntimeError):
    """Google sign-in could not be completed safely."""


def _redirect_uri() -> str:
    return f"{settings.backend_public_url.rstrip('/')}/api/auth/google/callback"


def _session_key() -> bytes:
    secret = settings.auth_session_secret or settings.google_client_secret
    if not secret:
        raise GoogleAuthError(
            "Configure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in backend/.env."
        )
    return hashlib.sha256(f"ai-dev-team-session:{secret}".encode("utf-8")).digest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def begin_google_sign_in() -> tuple[str, str, str]:
    if not settings.google_client_id or not settings.google_client_secret:
        raise GoogleAuthError(
            "Configure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in backend/.env."
        )
    _session_key()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": _redirect_uri(),
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}", state, verifier


def finish_google_sign_in(code: str, state: str, expected_state: str, verifier: str) -> dict[str, str]:
    if not code or not state or not expected_state or not hmac.compare_digest(state, expected_state):
        raise GoogleAuthError("Google sign-in expired or could not be verified. Please try again.")
    if not verifier:
        raise GoogleAuthError("Google sign-in expired or could not be verified. Please try again.")

    try:
        token_response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": _redirect_uri(),
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
        access_token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
        if not access_token:
            raise GoogleAuthError("Google did not return an access token.")

        profile_response = requests.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        profile_response.raise_for_status()
        profile = profile_response.json()
    except GoogleAuthError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise GoogleAuthError("Google sign-in could not be verified. Please try again.") from exc

    if not isinstance(profile, dict) or not profile.get("sub"):
        raise GoogleAuthError("Google did not return a valid account profile.")
    email = str(profile.get("email") or "").strip().lower()
    verified = profile.get("email_verified") is True or str(profile.get("email_verified")).lower() == "true"
    if not email or not verified:
        raise GoogleAuthError("Use a Google account with a verified email address.")

    return {
        "sub": str(profile["sub"]),
        "email": email,
        "name": str(profile.get("name") or email.split("@", 1)[0]),
        "picture": str(profile.get("picture") or ""),
    }


def create_session(user: dict[str, str]) -> str:
    payload = {
        "sub": user["sub"],
        "email": user["email"],
        "name": user["name"],
        "picture": user.get("picture", ""),
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(hmac.new(_session_key(), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def read_session(token: str | None) -> dict[str, str] | None:
    if not token:
        return None
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(_session_key(), encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        payload: Any = json.loads(_b64decode(encoded))
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= int(time.time()):
            return None
        if not payload.get("sub") or not payload.get("email"):
            return None
        return {
            "sub": str(payload["sub"]),
            "email": str(payload["email"]),
            "name": str(payload.get("name") or payload["email"]),
            "picture": str(payload.get("picture") or ""),
        }
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError, GoogleAuthError):
        return None
