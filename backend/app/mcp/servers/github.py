"""MCP server exposing GitHub tools over stdio.

Run standalone with ``python -m app.mcp.servers.github``.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.integrations.github import GitHubClient

mcp = FastMCP("github", log_level="WARNING")
_client = GitHubClient()


@mcp.tool(name="get_repository", description="Fetch repository metadata for an owner/repo pair.")
def get_repository(owner: str, repo: str) -> dict[str, Any]:
    return _client.get_repository(owner, repo)


@mcp.tool(name="list_pull_requests", description="List pull requests for an owner/repo pair.")
def list_pull_requests(owner: str, repo: str, state: str = "open") -> dict[str, Any]:
    return _client.list_pull_requests(owner, repo, state)


@mcp.tool(name="create_issue", description="Create a GitHub issue. Side-effecting.")
def create_issue(owner: str, repo: str, title: str, body: str) -> dict[str, Any]:
    return _client.create_issue(owner, repo, title, body)


if __name__ == "__main__":
    mcp.run()
