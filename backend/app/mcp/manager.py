"""Synchronous facade over persistent MCP stdio sessions.

The MCP SDK is async-only. The dev-team graph runs synchronously inside a
FastAPI route, so this module owns a dedicated background thread running an
asyncio event loop and keeps one persistent session per MCP server alive on it.
Sync callers submit coroutines to that loop and block on the result.

Sessions are opened lazily on first use and reused afterwards, so the ~1s
interpreter start-up cost is paid once per server per process.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession

from app.mcp.client import SERVER_NAMES, extract_result, stdio_parameters

_DEFAULT_TIMEOUT = 20.0
_MAX_START_ATTEMPTS = 2


class _Connection:
    __slots__ = ("stack", "session", "tools", "error", "attempts")

    def __init__(self) -> None:
        self.stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tools: list[str] = []
        self.error: str | None = None
        self.attempts = 0


class MCPClientManager:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._connections: dict[str, _Connection] = {}
        self._server_locks: dict[str, asyncio.Lock] = {}
        self._started = False
        self._registered_atexit = False

    # -- lifecycle ---------------------------------------------------------
    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._loop_main, args=(loop,), name="mcp-event-loop", daemon=True
            )
            thread.start()
            self._loop = loop
            self._thread = thread
            self._started = True
            if not self._registered_atexit:
                atexit.register(self.shutdown)
                self._registered_atexit = True

    @staticmethod
    def _loop_main(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    async def _aclose_all(self) -> None:
        for conn in list(self._connections.values()):
            if conn.stack is not None:
                try:
                    await conn.stack.aclose()
                except Exception:
                    pass
        self._connections.clear()

    def shutdown(self) -> None:
        if not self._started or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._aclose_all(), self._loop).result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._started = False

    # -- session management (runs on the background loop) ------------------
    def _lock_for(self, server: str) -> asyncio.Lock:
        lock = self._server_locks.get(server)
        if lock is None:
            lock = asyncio.Lock()
            self._server_locks[server] = lock
        return lock

    async def _get_session(self, server: str) -> ClientSession:
        conn = self._connections.get(server)
        if conn is None:
            conn = _Connection()
            self._connections[server] = conn
        if conn.session is not None:
            return conn.session
        # Allow a couple of attempts: a first start can fail transiently.
        if conn.error is not None and conn.attempts >= _MAX_START_ATTEMPTS:
            raise RuntimeError(conn.error)

        async with self._lock_for(server):
            if conn.session is not None:
                return conn.session
            if conn.error is not None and conn.attempts >= _MAX_START_ATTEMPTS:
                raise RuntimeError(conn.error)
            from mcp.client.stdio import stdio_client

            stack = AsyncExitStack()
            await stack.__aenter__()
            try:
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(stdio_parameters(server))
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                listing = await session.list_tools()
                conn.tools = [tool.name for tool in listing.tools]
            except Exception as exc:
                conn.error = str(exc)
                conn.attempts += 1
                try:
                    await stack.aclose()
                except Exception:
                    pass
                raise
            conn.stack = stack
            conn.session = session
            conn.error = None
            conn.attempts = 0
            return session

    async def _call(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        session = await self._get_session(server)
        result = await session.call_tool(tool, arguments)
        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool {server}.{tool} failed: {extract_result(result)}")
        return extract_result(result)

    async def _list_tools(self, server: str) -> list[dict[str, str]]:
        session = await self._get_session(server)
        listing = await session.list_tools()
        return [{"name": tool.name, "description": tool.description or ""} for tool in listing.tools]

    # -- sync facade -------------------------------------------------------
    def _submit(self, coro: Any, timeout: float) -> Any:
        self._ensure_started()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any], timeout: float = _DEFAULT_TIMEOUT
    ) -> Any:
        return self._submit(self._call(server, tool, arguments), timeout)

    def list_tools(self, server: str, timeout: float = 30.0) -> list[dict[str, str]]:
        return self._submit(self._list_tools(server), timeout)

    def server_status(self, server: str, timeout: float = 30.0) -> dict[str, Any]:
        try:
            tools = self.list_tools(server, timeout)
            return {"server": server, "status": "ok", "tools": tools}
        except Exception as exc:
            return {"server": server, "status": "error", "error": str(exc), "tools": []}

    def all_status(self, timeout: float = 30.0) -> list[dict[str, Any]]:
        return [self.server_status(name, timeout) for name in SERVER_NAMES]


_manager: MCPClientManager | None = None
_manager_lock = threading.Lock()


def get_mcp_manager() -> MCPClientManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = MCPClientManager()
    return _manager
