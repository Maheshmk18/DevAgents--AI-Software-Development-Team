"""Tests for the database availability cache.

Mongo is optional: when it is unreachable the app falls back to an in-memory
store. The important behaviour is that the fallback decision is *cached* — if it
were probed on every operation, each request would block for the full
server-selection timeout and the whole API would feel broken.
"""

from __future__ import annotations

import pytest

from app import db


@pytest.fixture(autouse=True)
def reset_probe_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "_memory_mode", None)
    monkeypatch.setattr(db, "_last_probe_at", 0.0)


class _CountingAdmin:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.calls = 0

    def command(self, name: str) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("mongo is down")
        return {"ok": 1}


class _CountingClient:
    def __init__(self, fail: bool) -> None:
        self.admin = _CountingAdmin(fail)


def test_probe_result_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _CountingClient(fail=True)
    monkeypatch.setattr(db, "client", client)

    # First call probes; the following calls reuse the cached answer.
    for _ in range(5):
        assert db._use_memory() is True
    assert client.admin.calls == 1


def test_probe_runs_again_after_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _CountingClient(fail=False)
    monkeypatch.setattr(db, "client", client)

    assert db._use_memory() is False
    assert client.admin.calls == 1
    assert db._use_memory() is False
    assert client.admin.calls == 1

    # Simulate the TTL expiring.
    monkeypatch.setattr(db, "_last_probe_at", db._last_probe_at - db._PROBE_TTL_SECONDS - 1)
    assert db._use_memory() is False
    assert client.admin.calls == 2


def test_memory_store_round_trips_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "client", _CountingClient(fail=True))
    db._MEMORY_FALLBACK["runs"].pop("__test__", None)

    db.insert_run({"id": "__test__", "status": "created"})
    assert db.get_run("__test__")["status"] == "created"

    # Re-persisting the same run must overwrite, not duplicate.
    db.insert_run({"id": "__test__", "status": "completed"})
    assert db.get_run("__test__")["status"] == "completed"
    assert db.list_runs().count(db.get_run("__test__")) == 1

    db._MEMORY_FALLBACK["runs"].pop("__test__", None)
