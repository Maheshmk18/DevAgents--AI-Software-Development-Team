"""The agents must never have to guess which GitHub repository to use.

The LLM has no way of knowing the user's repository, so left alone it calls the
github.* tools with ``owner="placeholder"`` and gets a 404. These tests pin the
two halves of the fix: the schema advertises the configured repository, and the
call path fills in (or repairs) ``owner``/``repo`` before dispatch.
"""

from __future__ import annotations

import pytest
import asyncio

from app.api.connectors_routes import GitHubRepositorySelection, select_github_repository
from app.config import settings
from app.integrations.github import GitHubClient
from app.mcp.tool_registry import (
    MCPToolRegistry,
    default_repo_parts,
    resolve_repo_arguments,
)
from app.services.agent_run_context import agent_run_context
from app.services import credential_store

SLUG = "acme/widget"


@pytest.fixture
def repo_configured(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "github_repo", SLUG)
    return SLUG


def _schema(agent: str, function_name: str) -> dict:
    registry = MCPToolRegistry(use_mcp=False)
    schemas = {
        item["function"]["name"]: item for item in registry.get_tool_schemas_for_agent(agent)
    }
    return schemas[function_name]


def test_default_repo_parts_parses_the_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_repo", "  acme/widget/  ")
    assert default_repo_parts() == ("acme", "widget")

    monkeypatch.setattr(settings, "github_repo", "")
    assert default_repo_parts() == ("", "")

    monkeypatch.setattr(settings, "github_repo", "just-a-name")
    assert default_repo_parts() == ("", "")


def test_placeholder_owner_and_repo_are_replaced(repo_configured: str) -> None:
    resolved = resolve_repo_arguments(
        "github.list_pull_requests",
        {"owner": "placeholder", "repo": "placeholder", "state": "open"},
    )
    assert resolved == {"owner": "acme", "repo": "widget", "state": "open"}


def test_missing_owner_and_repo_are_filled_in(repo_configured: str) -> None:
    assert resolve_repo_arguments("github.get_repository", {}) == {"owner": "acme", "repo": "widget"}


def test_agent_cannot_override_the_selected_repository(repo_configured: str) -> None:
    resolved = resolve_repo_arguments("github.get_repository", {"owner": "other", "repo": "thing"})
    assert resolved == {"owner": "acme", "repo": "widget"}


def test_run_repository_is_pinned_even_when_ui_selection_changes(repo_configured: str) -> None:
    with agent_run_context("C:/tmp/acme-widget-run", "acme/widget"):
        resolved = resolve_repo_arguments(
            "github.list_pull_requests",
            {"owner": "other", "repo": "another-repo", "state": "open"},
        )
    assert resolved == {"owner": "acme", "repo": "widget", "state": "open"}


def test_non_github_tools_are_untouched(repo_configured: str) -> None:
    arguments = {"owner": "placeholder"}
    assert resolve_repo_arguments("repo.list_files", arguments) is arguments


def test_schema_advertises_the_default_repository(repo_configured: str) -> None:
    schema = _schema("planner", "github__get_repository")
    assert SLUG in schema["function"]["description"]
    required = schema["function"]["parameters"]["required"]
    assert "owner" not in required
    assert "repo" not in required


def test_schema_still_requires_owner_and_repo_without_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_repo", "")
    required = _schema("planner", "github__get_repository")["function"]["parameters"]["required"]
    assert "owner" in required
    assert "repo" in required


def test_github_repository_can_be_changed_with_environment_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "configured-token")
    monkeypatch.setattr(credential_store, "get_credentials", lambda provider: None)
    monkeypatch.setattr(
        GitHubClient,
        "get_repository",
        lambda self, owner, repo: {"owner": owner, "repo": repo},
    )
    saved: list[dict] = []
    monkeypatch.setattr(
        credential_store,
        "save_credentials",
        lambda provider, credentials: saved.append(dict(credentials)),
    )

    result = asyncio.run(select_github_repository(GitHubRepositorySelection(slug="acme/new-widget")))

    assert result == {"slug": "acme/new-widget", "status": "selected"}
    assert saved == [{"default_repo": "acme/new-widget"}]


def test_environment_github_connection_exposes_repo_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.connection_service import _github

    monkeypatch.setattr(settings, "github_token", "configured-token")
    monkeypatch.setattr(settings, "github_repo", "acme/widget")
    monkeypatch.setattr(credential_store, "get_credentials", lambda provider: None)

    status = _github(storage_available=True)

    assert status["source"] == "environment"
    assert status["repository_selection_enabled"] is True
    assert status["slug"] == "acme/widget"
