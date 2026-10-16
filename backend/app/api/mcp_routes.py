from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from app.mcp.client import SERVER_NAMES
from app.mcp.tool_registry import MCPToolRegistry, SERVER_FOR_TOOL

router = APIRouter(prefix="/api/mcp", tags=["mcp"])
registry = MCPToolRegistry()


@router.get("/servers")
async def list_servers(probe: bool = True) -> dict[str, Any]:
    """List the MCP servers and (optionally) probe them for their live tools."""
    return await run_in_threadpool(registry.mcp_status, probe)


@router.get("/servers/{server_name}/tools")
async def server_tools(server_name: str) -> dict[str, Any]:
    if server_name not in SERVER_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown MCP server: {server_name}")
    status = await run_in_threadpool(registry.manager.server_status, server_name)
    if status.get("status") != "ok":
        raise HTTPException(status_code=502, detail=status.get("error", "MCP server unavailable"))
    return status


@router.get("/tools")
async def list_mcp_tool_map() -> dict[str, Any]:
    """Show which MCP server owns each tool, and whether it is read-only."""
    return {
        "enabled": registry.use_mcp,
        "tools": [
            {
                "tool": tool_name,
                "server": server,
                "read_only": MCPToolRegistry.is_read_only(tool_name),
                "side_effecting": MCPToolRegistry.is_side_effecting(tool_name),
            }
            for tool_name, server in sorted(SERVER_FOR_TOOL.items())
        ],
    }
