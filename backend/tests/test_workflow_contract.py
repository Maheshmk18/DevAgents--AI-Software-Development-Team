"""Contract tests for the dev-team workflow and its tool-safety policy.

These lock in the behaviours that matter for safety and orchestration:

* the graph shape (five agents plus a non-agent finalizer, no browser node),
* the bounded reviewer → developer retry loop,
* the role-scoped tool policy (read-only tools auto-run, side-effecting tools
  are only ever *proposed*), and
* the deferred-action contract produced by a full run.
"""

from __future__ import annotations

import pytest

from app.mcp.tool_registry import MCPToolRegistry
from app.services.execution_service import ExecutionService
from app.workflow.graph import (
    MAX_REVIEW_ATTEMPTS,
    build_workflow,
    route_after_review,
)

AGENTS = ("planner", "developer", "reviewer", "tester", "docs")


@pytest.fixture(scope="module")
def registry() -> MCPToolRegistry:
    return MCPToolRegistry(use_mcp=False)


@pytest.mark.parametrize("agent", AGENTS)
def test_role_tools_respect_safety_policy(registry: MCPToolRegistry, agent: str) -> None:
    """No side-effecting tool may be auto-run, and no read-only tool is deferred."""
    names = registry.get_tools_for_agent(agent)
    assert names, f"{agent} has no tools configured"

    auto_run = [name for name in names if registry.is_read_only(name)]
    deferred = [name for name in names if registry.is_side_effecting(name)]

    assert all(registry.is_read_only(name) for name in auto_run)
    assert all(registry.is_side_effecting(name) for name in deferred)
    assert set(auto_run) | set(deferred) == set(names)


def test_roles_may_propose_the_expected_actions(registry: MCPToolRegistry) -> None:
    assert "repo.write_file" in registry.get_tools_for_agent("developer")
    assert "repo.run_command" in registry.get_tools_for_agent("developer")
    assert "repo.run_command" in registry.get_tools_for_agent("reviewer")
    assert "repo.run_command" in registry.get_tools_for_agent("tester")
    assert "repo.write_file" in registry.get_tools_for_agent("docs")
    assert "gmail.send_email" in registry.get_tools_for_agent("docs")


def test_no_browser_tool_survives_anywhere(registry: MCPToolRegistry) -> None:
    catalog = {item["name"] for item in registry.list_tool_catalog()}
    assert not any("browser" in name or "playwright" in name for name in catalog)


def test_graph_shape_has_agents_and_non_agent_finalizer() -> None:
    nodes = set(build_workflow().get_graph().nodes)
    assert {"planner", "developer", "reviewer", "tester", "docs", "finalize"} <= nodes
    assert not any("browser" in name for name in nodes)


def test_retry_loop_is_bounded() -> None:
    assert MAX_REVIEW_ATTEMPTS == 2
    assert route_after_review({"review_passed": True, "review_attempts": 0}) == "tester"
    assert route_after_review({"review_passed": False, "review_attempts": 0}) == "developer"
    assert (
        route_after_review({"review_passed": False, "review_attempts": MAX_REVIEW_ATTEMPTS})
        == "tester"
    )


def test_full_run_defers_only_side_effecting_actions(registry: MCPToolRegistry) -> None:
    result = ExecutionService().execute_run(
        {"id": "test-run", "message": "Build a small CLI tool"}
    )

    stages = [entry["agent"] for entry in result["stage_results"]]
    assert stages and stages[0] == "planner"
    assert result["final_answer"]

    deferred = result["deferred_actions"]
    assert deferred, "a typical run should propose at least one action"
    assert all(registry.is_side_effecting(action["tool"]) for action in deferred)

    tools = {action["tool"] for action in deferred}
    assert "repo.write_file" in tools
    assert "repo.run_command" in tools

    # Identical (tool, arguments) proposals from different agents collapse to one.
    keys = [
        (action["tool"], sorted((action.get("arguments") or {}).items()))
        for action in deferred
    ]
    assert len(keys) == len(set(map(str, keys)))

    assert not any("browser" in (event.get("tool") or "") for event in result["tool_events"])
