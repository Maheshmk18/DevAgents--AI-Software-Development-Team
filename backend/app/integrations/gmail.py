from __future__ import annotations

import base64
import time
from email.message import EmailMessage
from typing import Any

import requests

from app.config import settings
from app.integrations.errors import (
    IntegrationAuthError,
    IntegrationError,
    IntegrationNotConfigured,
)
from app.services import credential_store
from app.services.credential_store import CredentialStoreError

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

#: ``gmail_sender_email`` ships with this placeholder; treat it as "unset" so we
#: never put a fake address in the From header.
_PLACEHOLDER_SENDER = "you@example.com"


class GmailClient:
    """Gmail REST client with an OAuth 2.0 refresh-token flow.

    A pasted access token dies after ~1 hour, which made "send an email" work
    once and then fail forever. When ``GMAIL_REFRESH_TOKEN`` (plus a client id
    and secret) is configured we mint a fresh access token on demand and cache
    it until it actually expires.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0

    # ------------------------------------------------------------------ auth

    def _access_token(self) -> str:
        try:
            credentials = credential_store.get_credentials("gmail")
        except CredentialStoreError as exc:
            credentials = None
            if not settings.gmail_refresh_token and not settings.gmail_access_token:
                raise IntegrationNotConfigured(str(exc)) from exc

        if credentials:
            access_token = str(credentials.get("access_token") or "")
            expires_at = credentials.get("expires_at")
            if access_token and (not expires_at or time.time() < float(expires_at) - 60):
                return access_token
            if credentials.get("refresh_token"):
                return self._refresh_access_token(credentials)
            raise IntegrationAuthError("Gmail access expired. Reconnect Gmail from the console.")

        if settings.gmail_refresh_token and settings.google_client_id and settings.google_client_secret:
            if self._token and time.time() < self._expires_at:
                return self._token
            return self._refresh_access_token()
        if settings.gmail_access_token:
            return settings.gmail_access_token
        raise IntegrationNotConfigured(
            "Gmail is not connected. Set GMAIL_REFRESH_TOKEN (recommended) with "
            "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET, or a short-lived "
            "GMAIL_ACCESS_TOKEN, in backend/.env and restart the backend."
        )

    def _refresh_access_token(self, credentials: dict[str, Any] | None = None) -> str:
        refresh_token = (
            str(credentials.get("refresh_token") or "")
            if credentials is not None
            else settings.gmail_refresh_token
        )
        if not refresh_token or not settings.google_client_id or not settings.google_client_secret:
            raise IntegrationNotConfigured("Gmail refresh-token credentials are incomplete. Reconnect Gmail.")
        try:
            response = requests.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            raise IntegrationError(f"Gmail token refresh failed: {exc}") from exc

        if response.status_code != 200:
            raise IntegrationAuthError(
                "Gmail rejected the stored refresh token "
                f"({response.status_code}). Re-run the OAuth consent flow and update "
                "GMAIL_REFRESH_TOKEN in backend/.env."
            )

        try:
            payload = response.json()
            access_token = str(payload["access_token"])
        except (ValueError, KeyError, TypeError) as exc:
            raise IntegrationAuthError("Gmail returned an unreadable token refresh response.") from exc
        # Refresh a minute early so a request never races the expiry.
        expires_at = time.time() + int(payload.get("expires_in", 3600)) - 60
        if credentials is not None:
            credentials["access_token"] = access_token
            credentials["expires_at"] = expires_at
            if payload.get("refresh_token"):
                credentials["refresh_token"] = payload["refresh_token"]
            credential_store.save_credentials("gmail", credentials)
        else:
            self._token = access_token
            self._expires_at = expires_at
        return access_token

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        token = self._access_token()
        try:
            response = requests.request(
                method,
                f"{GMAIL_API}{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise IntegrationError(f"Gmail request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise IntegrationAuthError(
                f"Gmail rejected the token ({response.status_code}). The credential "
                "is missing, expired, or lacks the Gmail scope."
            )
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise IntegrationError(f"Gmail request failed: {exc}") from exc
        return response.json()

    # ----------------------------------------------------------------- tools

    def list_messages(self, query: str = "", max_results: int = 5) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/messages",
            params={"q": query, "maxResults": max_results},
        )
        return {
            "query": query,
            "count": len(payload.get("messages", [])),
            "messages": payload.get("messages", []),
            "source": "gmail",
        }

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str | None = None,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> dict[str, Any]:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        if cc:
            message["Cc"] = cc
        try:
            credentials = credential_store.get_credentials("gmail")
        except CredentialStoreError:
            credentials = None
        # OAuth verifies the mailbox identity during setup. Prefer it over a
        # legacy .env sender value so an unrelated configured alias cannot make
        # Gmail reject the message's From header.
        if credentials and credentials.get("access_token"):
            sender = str(credentials.get("account") or "").strip()
        else:
            sender = settings.gmail_sender_email.strip()
        if sender and sender != _PLACEHOLDER_SENDER:
            message["From"] = sender
        message.set_content(body)
        total_attachment_bytes = 0
        for filename, content, content_type in attachments or []:
            if not filename or "/" in filename or "\\" in filename:
                raise ValueError("Gmail attachment filename must be a plain filename.")
            total_attachment_bytes += len(content)
            if total_attachment_bytes > 10 * 1024 * 1024:
                raise ValueError("Gmail report attachments must be 10 MB or smaller.")
            maintype, separator, subtype = content_type.partition("/")
            if not separator or not maintype or not subtype:
                raise ValueError("Gmail attachment content type is invalid.")
            message.add_attachment(
                content,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )

        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        result = self._request("POST", "/messages/send", json={"raw": encoded})
        return {
            "status": "sent",
            "provider": "gmail",
            "source": "gmail",
            "message_id": result.get("id"),
            "to": to,
            "subject": subject,
        }
