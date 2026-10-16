"""Shared test fixtures.

Tests are hermetic: they never make real LLM calls, never spawn MCP
subprocesses, and never touch a real database. The LLM is disabled by default
(so agents exercise their deterministic fallback) and Mongo always looks
unreachable (so the in-memory store is used); tests that want the LLM or Mongo
path stub those collaborators themselves.
"""

from __future__ import annotations

import pytest

from app import db
from app.config import settings
from app.services.llm_service import llm_service


class _OfflineAdmin:
    def command(self, name: str) -> dict:
        raise RuntimeError("database disabled in tests")


class _OfflineClient:
    admin = _OfflineAdmin()


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # No provider keys -> llm_service.get_chat_model() returns None.
    monkeypatch.setattr(llm_service, "provider_chain", lambda: [])
    monkeypatch.setattr(llm_service, "last_provider", None)
    monkeypatch.setattr(llm_service, "last_error", None)
    # Clear any provider cooldown left over from another test.
    monkeypatch.setattr(llm_service, "_unavailable_until", 0.0)

    # Route every tool call through the in-process clients instead of stdio MCP.
    monkeypatch.setattr(settings, "mcp_enabled", False)

    # Make Mongo look unreachable without any network I/O, so the DB helpers
    # take their in-memory path immediately.
    monkeypatch.setattr(db, "client", _OfflineClient())
    monkeypatch.setattr(db, "_memory_mode", None)
    monkeypatch.setattr(db, "_last_probe_at", 0.0)
