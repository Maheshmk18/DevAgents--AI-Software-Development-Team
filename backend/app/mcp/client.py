"""Low-level MCP client primitives.

The servers live under ``app.mcp.servers`` and are launched as stdio
subprocesses. Everything here is async; the synchronous facade that the rest of
the app uses lives in :mod:`app.mcp.manager`.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

#: Repository root of the backend package (…/backend).
BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Server names that exist under ``app.mcp.servers``.
SERVER_NAMES: tuple[str, ...] = ("repo", "github", "gmail")

#: Environment variables a stdio MCP server needs on Windows to start.
_INHERITED_ENV = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "COMSPEC",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
)


def server_environment() -> dict[str, str]:
    """Build the environment for a stdio MCP server subprocess."""
    from app.config import settings

    environment = {key: os.environ[key] for key in _INHERITED_ENV if key in os.environ}
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONPATH"] = str(BACKEND_ROOT)
    # Credentials / workspace are forwarded so the servers behave like the backend.
    environment["REPO_ROOT"] = settings.repo_root
    environment["GMAIL_ACCESS_TOKEN"] = settings.gmail_access_token
    environment["GMAIL_SENDER_EMAIL"] = settings.gmail_sender_email
    environment["GITHUB_TOKEN"] = settings.github_token
    return environment


def stdio_parameters(server_name: str) -> StdioServerParameters:
    if server_name not in SERVER_NAMES:
        raise ValueError(f"Unknown MCP server: {server_name}")
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", f"app.mcp.servers.{server_name}"],
        env=server_environment(),
        cwd=str(BACKEND_ROOT),
    )


def extract_result(result: Any) -> Any:
    """Unwrap a ``CallToolResult`` into plain Python data.

    Every tool in this project returns a dict, which FastMCP serialises to JSON
    text in the first content block.
    """
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    texts = [
        getattr(block, "text", "")
        for block in getattr(result, "content", [])
        if getattr(block, "type", None) == "text"
    ]
    text = "\n".join(part for part in texts if part)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


@asynccontextmanager
async def open_mcp_session(server_name: str) -> AsyncIterator[ClientSession]:
    """Open a one-shot MCP session against a stdio server."""
    async with stdio_client(stdio_parameters(server_name)) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session
