from __future__ import annotations

from typing import Any

import requests

from app.config import settings


class GitHubClient:
    def __init__(self) -> None:
        self.base_url = "https://api.github.com"

    def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        if not settings.github_token:
            return {
                "owner": owner,
                "repo": repo,
                "default_branch": "main",
                "visibility": "private",
                "source": "demo",
                "summary": "Demo repository metadata used when no GitHub token is configured.",
            }

        response = requests.get(
            f"{self.base_url}/repos/{owner}/{repo}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {settings.github_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        return {
            "owner": payload.get("owner", {}).get("login", owner),
            "repo": payload.get("name", repo),
            "default_branch": payload.get("default_branch", "main"),
            "visibility": payload.get("visibility", "private"),
            "source": "github",
            "summary": payload.get("description", "Repository available through GitHub API."),
        }

    def list_pull_requests(self, owner: str, repo: str, state: str = "open") -> dict[str, Any]:
        if not settings.github_token:
            return {
                "owner": owner,
                "repo": repo,
                "state": state,
                "count": 1,
                "pull_requests": [
                    {
                        "number": 1,
                        "title": "Demo PR: initial implementation",
                        "state": state,
                        "url": "https://github.com/example/repo/pull/1",
                    }
                ],
                "source": "demo",
            }

        response = requests.get(
            f"{self.base_url}/repos/{owner}/{repo}/pulls",
            params={"state": state},
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {settings.github_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        return {
            "owner": owner,
            "repo": repo,
            "state": state,
            "count": len(payload),
            "pull_requests": payload,
            "source": "github",
        }

    def create_issue(self, owner: str, repo: str, title: str, body: str) -> dict[str, Any]:
        if not settings.github_token:
            return {
                "owner": owner,
                "repo": repo,
                "title": title,
                "status": "simulated",
                "source": "demo",
                "issue_number": 999,
            }

        response = requests.post(
            f"{self.base_url}/repos/{owner}/{repo}/issues",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {settings.github_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "body": body},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        return {
            "owner": owner,
            "repo": repo,
            "title": title,
            "status": "created",
            "source": "github",
            "issue_number": payload.get("number"),
        }
