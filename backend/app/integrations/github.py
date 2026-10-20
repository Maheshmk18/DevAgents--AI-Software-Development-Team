from __future__ import annotations

import time
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


class GitHubClient:
    def __init__(self) -> None:
        self.base_url = "https://api.github.com"

    def _headers(self) -> dict[str, str]:
        token = self._access_token()
        if not token:
            raise IntegrationNotConfigured(
                "GitHub is not connected: GITHUB_TOKEN is empty. Add a GitHub "
                "personal access token to backend/.env and restart the backend."
            )
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _access_token(self) -> str:
        try:
            credentials = credential_store.get_credentials("github")
        except CredentialStoreError as exc:
            if settings.github_token:
                return settings.github_token
            raise IntegrationNotConfigured(str(exc)) from exc
        if not credentials:
            return settings.github_token

        expires_at = credentials.get("expires_at")
        access_token = str(credentials.get("access_token") or "")
        if not expires_at or float(expires_at) > time.time() + 60:
            return access_token

        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            raise IntegrationAuthError("GitHub access expired. Reconnect GitHub from the console.")
        if not settings.github_oauth_client_id or not settings.github_oauth_client_secret:
            raise IntegrationNotConfigured("GitHub OAuth client credentials are missing; reconnect GitHub.")
        try:
            response = requests.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": settings.github_oauth_client_id,
                    "client_secret": settings.github_oauth_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={"Accept": "application/json"},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise IntegrationError(f"GitHub token refresh failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise IntegrationAuthError("GitHub returned an unreadable token refresh response.") from exc
        if (
            response.status_code >= 400
            or not isinstance(payload, dict)
            or payload.get("error")
            or not payload.get("access_token")
        ):
            raise IntegrationAuthError("GitHub access expired. Reconnect GitHub from the console.")

        credentials["access_token"] = payload["access_token"]
        credentials["expires_at"] = time.time() + int(payload.get("expires_in", 28800))
        if payload.get("refresh_token"):
            credentials["refresh_token"] = payload["refresh_token"]
        if payload.get("refresh_token_expires_in"):
            credentials["refresh_expires_at"] = time.time() + int(payload["refresh_token_expires_in"])
        credential_store.save_credentials("github", credentials)
        return str(credentials["access_token"])

    def access_token(self) -> str:
        """Return a current token for an authenticated Git operation."""
        token = self._access_token()
        if not token:
            raise IntegrationNotConfigured(
                "GitHub is not connected. Reconnect the account from Integrations."
            )
        return token

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Perform an authenticated GitHub call, turning failures into clear errors."""
        try:
            response = requests.request(method, url, headers=self._headers(), timeout=15, **kwargs)
        except requests.RequestException as exc:
            raise IntegrationError(f"GitHub request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise IntegrationAuthError(
                f"GitHub rejected the token ({response.status_code}). Check that "
                "GITHUB_TOKEN is valid, unexpired, and can read this repository."
            )
        if response.status_code == 404:
            raise IntegrationError(
                "GitHub returned 404. Check the repository slug (owner/repo) and "
                "that GITHUB_TOKEN is allowed to see it."
            )
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise IntegrationError(f"GitHub request failed: {exc}") from exc
        return response.json()

    def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        payload = self._request("GET", f"{self.base_url}/repos/{owner}/{repo}")
        raw_permissions = payload.get("permissions")
        permissions = (
            {
                key: bool(raw_permissions[key]) if key in raw_permissions else None
                for key in ("pull", "push", "admin")
            }
            if isinstance(raw_permissions, dict)
            else None
        )
        return {
            "owner": payload.get("owner", {}).get("login", owner),
            "repo": payload.get("name", repo),
            "default_branch": payload.get("default_branch", "main"),
            "permissions": permissions,
            "visibility": payload.get("visibility", "private"),
            "language": payload.get("language"),
            "open_issues": payload.get("open_issues_count"),
            "source": "github",
            "summary": payload.get("description") or "Repository available through the GitHub API.",
        }

    def list_repositories(self) -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"{self.base_url}/user/repos",
            params={
                "affiliation": "owner,collaborator,organization_member",
                "per_page": 100,
                "sort": "updated",
            },
        )
        repositories = [
            {
                "full_name": item.get("full_name"),
                "private": bool(item.get("private")),
                "default_branch": item.get("default_branch"),
            }
            for item in payload
            if isinstance(item, dict) and item.get("full_name")
        ]
        return {"repositories": repositories, "count": len(repositories), "source": "github"}

    def list_pull_requests(self, owner: str, repo: str, state: str = "open") -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"{self.base_url}/repos/{owner}/{repo}/pulls",
            params={"state": state},
        )
        pull_requests = [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "author": (item.get("user") or {}).get("login"),
                "url": item.get("html_url"),
            }
            for item in payload
        ]
        return {
            "owner": owner,
            "repo": repo,
            "state": state,
            "count": len(pull_requests),
            "pull_requests": pull_requests,
            "source": "github",
        }

    def create_issue(self, owner: str, repo: str, title: str, body: str) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"{self.base_url}/repos/{owner}/{repo}/issues",
            json={"title": title, "body": body},
        )
        return {
            "owner": owner,
            "repo": repo,
            "title": title,
            "status": "created",
            "source": "github",
            "issue_number": payload.get("number"),
            "url": payload.get("html_url"),
        }

    def create_pull_request(
        self,
        *,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        """Open a PR for an approved branch, reusing it if a retry finds one."""
        payload: dict[str, Any] | None = None
        reused = False
        try:
            response = self._request(
                "POST",
                f"{self.base_url}/repos/{owner}/{repo}/pulls",
                json={
                    "head": head,
                    "base": base,
                    "title": title,
                    "body": body,
                    "draft": False,
                },
            )
            payload = response if isinstance(response, dict) else None
        except IntegrationError as exc:
            existing = self._request(
                "GET",
                f"{self.base_url}/repos/{owner}/{repo}/pulls",
                params={"state": "open", "head": f"{owner}:{head}"},
            )
            if isinstance(existing, list):
                payload = next(
                    (item for item in existing if isinstance(item, dict) and item.get("head", {}).get("ref") == head),
                    None,
                )
            if payload is None:
                raise exc
            reused = True
        if not payload:
            raise IntegrationError("GitHub did not return a pull request after branch publication.")
        return {
            "status": "created" if not reused else "already_open",
            "number": payload.get("number"),
            "title": payload.get("title") or title,
            "url": payload.get("html_url"),
            "head": (payload.get("head") or {}).get("ref", head),
            "base": (payload.get("base") or {}).get("ref", base),
            "source": "github",
        }
