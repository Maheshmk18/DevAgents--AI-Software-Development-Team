"""MCP server exposing workspace/repository tools over stdio.

Run standalone with ``python -m app.mcp.servers.repo``.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.mcp.repository import RepositoryMCPClient

mcp = FastMCP("repo", log_level="WARNING")
_client = RepositoryMCPClient()


@mcp.tool(name="list_files", description="List workspace files under a path, excluding build and VCS directories.")
def list_files(relative_path: str = ".", max_files: int = 200) -> dict[str, Any]:
    files = _client.list_files(relative_path, max_files)
    return {"files": files, "count": len(files)}


@mcp.tool(name="read_file", description="Read a UTF-8 file from the workspace and return its content.")
def read_file(file_path: str) -> dict[str, Any]:
    content = _client.read_file(file_path)
    return {"file_path": file_path, "content": content, "chars": len(content)}


@mcp.tool(name="write_file", description="Write UTF-8 content to a file in the workspace. Side-effecting.")
def write_file(file_path: str, content: str) -> dict[str, Any]:
    return _client.write_file(file_path, content)


@mcp.tool(name="search_files", description="Find workspace files whose name contains a query string.")
def search_files(query: str, relative_path: str = ".") -> dict[str, Any]:
    matches = _client.search_files(query, relative_path)
    return {"matches": matches, "count": len(matches)}


@mcp.tool(name="run_command", description="Run a shell command inside the workspace. Side-effecting.")
def run_command(command: str) -> dict[str, Any]:
    return _client.run_command(command)


if __name__ == "__main__":
    mcp.run()
