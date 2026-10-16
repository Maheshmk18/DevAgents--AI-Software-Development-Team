"""MCP integration for the dev team.

This package is deliberately **lazy**: the MCP server subprocesses
(``app.mcp.servers.*``) import this package first, and pulling in the full tool
registry here would make every server load the whole application graph. Import
the concrete modules you need (``app.mcp.tool_registry``, ``app.mcp.manager``,
…) instead of relying on re-exports.
"""

__all__: list[str] = []
