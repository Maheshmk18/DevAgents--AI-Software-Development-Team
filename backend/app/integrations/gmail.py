from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

import requests

from app.config import settings


class GmailClient:
    def list_messages(self, query: str = "", max_results: int = 5) -> dict[str, Any]:
        if not settings.gmail_access_token:
            return {
                "query": query,
                "count": 1,
                "messages": [
                    {
                        "id": "demo-message-1",
                        "subject": "Portfolio follow-up",
                        "from": "recruiter@example.com",
                        "snippet": "Thanks for the application. I would like to schedule a short discussion about the role.",
                    }
                ],
                "source": "demo",
            }

        response = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            params={"q": query, "maxResults": max_results},
            headers={"Authorization": f"Bearer {settings.gmail_access_token}"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        return {
            "query": query,
            "count": len(payload.get("messages", [])),
            "messages": payload.get("messages", []),
            "source": "gmail",
        }

    def send_email(self, to: str, subject: str, body: str, cc: str | None = None) -> dict[str, Any]:
        if not settings.gmail_access_token:
            return {
                "status": "simulated",
                "to": to,
                "subject": subject,
                "source": "demo",
                "message_id": "demo-message-id",
            }

        message = EmailMessage()
        message["To"] = to
        message["From"] = settings.gmail_sender_email
        message["Subject"] = subject
        if cc:
            message["Cc"] = cc
        message.set_content(body)

        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        payload = {"raw": encoded}
        response = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {settings.gmail_access_token}"},
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        result = response.json()
        return {
            "status": "sent",
            "provider": "gmail",
            "message_id": result.get("id"),
            "to": to,
            "subject": subject,
        }
