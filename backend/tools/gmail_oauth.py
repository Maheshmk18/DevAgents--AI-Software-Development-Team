"""One-off helper that mints a Gmail OAuth refresh token.

A pasted access token stops working after ~1 hour. A refresh token does not:
the backend exchanges it for short-lived access tokens automatically, so
``gmail.send_email`` keeps working.

Usage
-----
1. In the Google Cloud Console, create an OAuth client of type **Desktop app**
   and enable the **Gmail API**. Note the client id and secret.
2. Run this script from the ``backend`` directory:

       python -m tools.gmail_oauth --client-id <ID> --client-secret <SECRET>

   (or set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET first and omit the flags).

3. A browser window opens. Sign in as the Gmail account you want to send from
   and approve the requested scope.
4. Copy the printed ``GMAIL_REFRESH_TOKEN=...`` line into ``backend/.env`` and
   restart the backend.

Only the Gmail scopes are requested (send + read), so the token cannot touch
other Google services.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
#: ``gmail.send`` is what sending needs; ``gmail.readonly`` also powers the
#: ``gmail.list_messages`` tool. Requesting both means neither tool 403s.
SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)
REDIRECT_URI = "http://localhost:8765/"

_code: str | None = None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        global _code
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _code = (params.get("code") or [None])[0]
        error = (params.get("error") or [None])[0]

        body = "You can close this tab and return to the terminal."
        if error or not _code:
            body = f"Authorization failed: {error or 'no code returned'}"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args: object) -> None:  # silence the default logging
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a Gmail refresh token.")
    parser.add_argument("--client-id", default=os.environ.get("GOOGLE_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("GOOGLE_CLIENT_SECRET", ""))
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        print("Missing client id/secret. Pass --client-id/--client-secret or set them in .env.")
        return 2

    auth_query = urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    url = f"{AUTH_URL}?{auth_query}"

    print("Opening the Google consent page in your browser...")
    print(f"If it does not open, paste this URL manually:\n\n{url}\n")
    webbrowser.open(url)

    server = HTTPServer(("localhost", 8765), _Handler)
    server.handle_request()

    if not _code:
        print("No authorization code was received.")
        return 1

    response = requests.post(
        TOKEN_URL,
        data={
            "code": _code,
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        print(f"Token exchange failed ({response.status_code}): {response.text}")
        return 1

    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        print("Google did not return a refresh token. Re-run and approve access again.")
        return 1

    print("\nSuccess. Add these three lines to backend/.env:\n")
    print(f"GOOGLE_CLIENT_ID={args.client_id}")
    print(f"GOOGLE_CLIENT_SECRET={args.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={refresh_token}")
    print("\nThen restart the backend. Gmail sending will stay connected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
