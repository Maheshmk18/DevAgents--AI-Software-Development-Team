"""MCP server exposing Gmail tools over stdio.

Run standalone with ``python -m app.mcp.servers.gmail``.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.integrations.gmail import GmailClient

mcp = FastMCP("gmail", log_level="WARNING")
_client = GmailClient()


@mcp.tool(name="list_messages", description="List recent Gmail messages matching a query.")
def list_messages(query: str = "", max_results: int = 5) -> dict[str, Any]:
    return _client.list_messages(query, max_results)


@mcp.tool(name="send_email", description="Send an email from the configured account. Side-effecting.")
def send_email(to: str, subject: str, body: str, cc: str | None = None) -> dict[str, Any]:
    return _client.send_email(to, subject, body, cc)


if __name__ == "__main__":
    mcp.run()
