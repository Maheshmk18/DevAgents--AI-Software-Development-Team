"""The console must never show invented data, and history must be clearable.

Two regressions are pinned here:

* the integrations used to return fabricated "demo" payloads when a token was
  missing, which made the agents look connected while they invented results;
* every approval ever recorded was listed in the UI, so old runs piled up as
  duplicate-looking "static" cards.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.integrations.errors import IntegrationAuthError, IntegrationNotConfigured
from app.integrations.github import GitHubClient
from app.integrations.gmail import GmailClient


# --- integrations fail loudly instead of inventing data --------------------


def test_github_without_a_token_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "")
    with pytest.raises(IntegrationNotConfigured):
        GitHubClient().get_repository("acme", "widget")


def test_github_never_returns_demo_pull_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "")
    with pytest.raises(IntegrationNotConfigured):
        GitHubClient().list_pull_requests("acme", "widget")


def test_gmail_without_a_credential_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gmail_access_token", "")
    monkeypatch.setattr(settings, "gmail_refresh_token", "")
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    with pytest.raises(IntegrationNotConfigured):
        GmailClient().list_messages()


def test_github_auth_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "ghp_expired")

    class FakeResponse:
        status_code = 401

        def raise_for_status(self) -> None:  # pragma: no cover - not reached
            raise AssertionError("should not be called")

    monkeypatch.setattr(
        "app.integrations.github.requests.request", lambda *a, **k: FakeResponse()
    )

    with pytest.raises(IntegrationAuthError):
        GitHubClient().get_repository("acme", "widget")


# --- Gmail refresh-token flow ---------------------------------------------


def test_gmail_refreshes_and_caches_a_short_lived_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gmail_access_token", "")
    monkeypatch.setattr(settings, "gmail_refresh_token", "refresh-1")
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")

    refreshes: list[dict] = []

    class TokenResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"access_token": "fresh-access", "expires_in": 3600}

    monkeypatch.setattr(
        "app.integrations.gmail.requests.post",
        lambda url, data=None, timeout=None: refreshes.append(data) or TokenResponse(),
    )

    client = GmailClient()
    # Two calls, but the token is only minted once and then served from cache.
    assert client._access_token() == "fresh-access"
    assert client._access_token() == "fresh-access"
    assert len(refreshes) == 1
    assert refreshes[0]["grant_type"] == "refresh_token"
    assert refreshes[0]["refresh_token"] == "refresh-1"


def test_gmail_reports_a_rejected_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gmail_access_token", "")
    monkeypatch.setattr(settings, "gmail_refresh_token", "stale")
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")

    class TokenResponse:
        status_code = 400

        @staticmethod
        def json() -> dict:  # pragma: no cover - not reached
            return {}

    monkeypatch.setattr(
        "app.integrations.gmail.requests.post",
        lambda url, data=None, timeout=None: TokenResponse(),
    )

    with pytest.raises(IntegrationAuthError):
        GmailClient()._access_token()


# --- connection status -----------------------------------------------------


def test_connection_status_reports_where_each_credential_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.connection_service import connection_status

    monkeypatch.setattr(settings, "github_token", "ghp_x")
    monkeypatch.setattr(settings, "github_repo", "acme/widget")

    status = connection_status()

    assert status["github"]["connected"] is True
    assert status["github"]["slug"] == "acme/widget"
    assert status["github"]["tone"] == "ok"
    assert "GITHUB_TOKEN" in status["github"]["where"]
    # Secrets must never be exposed by the status view.
    assert "ghp_x" not in str(status)


def test_connection_status_flags_a_short_lived_gmail_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.connection_service import connection_status

    monkeypatch.setattr(settings, "gmail_refresh_token", "")
    monkeypatch.setattr(settings, "gmail_access_token", "ya29.expired")

    status = connection_status()

    # Configured, but a pasted token dies after ~1 hour: warn, do not claim ok.
    assert status["gmail"]["connected"] is True
    assert status["gmail"]["tone"] == "warn"
    assert status["gmail"]["note"]


def test_connection_status_prefers_the_refresh_token_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.connection_service import connection_status

    monkeypatch.setattr(settings, "gmail_access_token", "")
    monkeypatch.setattr(settings, "gmail_refresh_token", "refresh")
    monkeypatch.setattr(settings, "google_client_id", "cid")
    monkeypatch.setattr(settings, "google_client_secret", "secret")

    status = connection_status()

    assert status["gmail"]["mode"] == "refresh_token"
    assert status["gmail"]["tone"] == "ok"
    assert status["gmail"]["note"] is None


def test_connection_status_reports_mongo_as_down_when_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Mongo row must reflect reality, not a hardcoded 'ok'."""
    import app.db as db_module
    from app.services.connection_service import connection_status

    monkeypatch.setattr(db_module, "database_status", lambda: {"status": "error", "database": "x"})

    status = connection_status()

    assert status["mongo"]["connected"] is False
    assert status["mongo"]["tone"] == "off"


# --- run history is scoped and clearable ----------------------------------


def test_clearing_the_run_history_removes_runs_and_their_approvals() -> None:
    from app.db import _MEMORY_FALLBACK, delete_all_runs, insert_approval, insert_run
    from app.services.workflow_service import workflow_service

    _MEMORY_FALLBACK["runs"].clear()
    _MEMORY_FALLBACK["approvals"].clear()
    _MEMORY_FALLBACK["evaluations"].clear()

    insert_run({"id": "run-a", "status": "completed"})
    insert_run({"id": "run-b", "status": "completed"})
    insert_approval({"id": "ap-1", "run_id": "run-a", "action": "approve_plan_before_execution"})

    assert delete_all_runs() == 2
    assert _MEMORY_FALLBACK["runs"] == {}
    assert _MEMORY_FALLBACK["approvals"] == {}
    # The service's in-memory mirrors are cleared too, or the UI would keep them.
    workflow_service.clear_runs()
    assert workflow_service.runs == {}


def test_deleting_one_run_leaves_the_others_alone() -> None:
    from app.db import _MEMORY_FALLBACK, delete_run, insert_approval, insert_run

    _MEMORY_FALLBACK["runs"].clear()
    _MEMORY_FALLBACK["approvals"].clear()

    insert_run({"id": "keep-me", "status": "completed"})
    insert_run({"id": "drop-me", "status": "completed"})
    insert_approval({"id": "ap-1", "run_id": "keep-me", "action": "x"})
    insert_approval({"id": "ap-2", "run_id": "drop-me", "action": "x"})

    delete_run("drop-me")

    assert set(_MEMORY_FALLBACK["runs"]) == {"keep-me"}
    assert set(_MEMORY_FALLBACK["approvals"]) == {"ap-1"}
