"""Shared test fixtures.

Tests are hermetic: they never make real LLM calls, never spawn MCP
subprocesses, and never touch a real database. The LLM is disabled by default
(so agents exercise their deterministic fallback) and Mongo always looks
unreachable (so the in-memory store is used); tests that want the LLM or Mongo
path stub those collaborators themselves.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Settings loads ``backend/.env`` during import. A developer's local file may
# contain an Atlas ``mongodb+srv://`` URL, whose DNS lookup happens inside
# ``MongoClient`` before fixtures can replace the client. Give tests a local,
# non-resolving URI before importing the application; the fixture below then
# swaps in the offline client before any test can use it.
os.environ["MONGO_URI"] = "mongodb://127.0.0.1:27017"
_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

# Keep pytest's temporary workspace short. Some tests create nested Git
# repositories, and rooting those under a workflow clone can exceed Windows'
# path limit while Git writes objects into ``.git/objects``.
_SYSTEM_TEMP_ROOT = Path(tempfile.gettempdir())
_TEST_TEMP_ROOT = _SYSTEM_TEMP_ROOT / f"adpytest-{os.getpid()}"
_TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
for _temp_var in ("TEMP", "TMP", "TMPDIR"):
    os.environ[_temp_var] = str(_TEST_TEMP_ROOT)
tempfile.tempdir = str(_TEST_TEMP_ROOT)

import pytest

from app import db
from app.config import settings
from app.services import credential_store
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

    # Do not let tests read real OAuth tokens from Windows Credential Manager.
    # Individual tests can stub this function when they need stored credentials.
    monkeypatch.setattr(credential_store, "get_credentials", lambda provider: None)

    # Make Mongo look unreachable without any network I/O, so the DB helpers
    # take their in-memory path immediately.
    monkeypatch.setattr(db, "client", _OfflineClient())
    monkeypatch.setattr(db, "_memory_mode", None)
    monkeypatch.setattr(db, "_last_probe_at", 0.0)
