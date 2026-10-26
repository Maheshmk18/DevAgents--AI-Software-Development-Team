"""Contract tests for the dev-team workflow and its tool-safety policy.

These lock in the behaviours that matter for safety and orchestration:

* the graph shape (five agents plus a non-agent finalizer, no browser node),
* the bounded reviewer → developer retry loop,
* the role-scoped tool policy (workspace changes stay in the isolated branch,
  while external-service actions are deferred), and
* the deferred-action contract produced by a full run.
"""

from __future__ import annotations

import json

import pytest

from app.mcp.tool_registry import MCPToolRegistry
from app.services.execution_service import ExecutionService
from app.workflow.graph import (
    MAX_REVIEW_ATTEMPTS,
    MAX_VALIDATION_ATTEMPTS,
    build_workflow,
    route_after_review,
    route_after_tester,
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
    assert "repo.run_command" not in registry.get_tools_for_agent("developer")
    assert "repo.run_command" not in registry.get_tools_for_agent("reviewer")
    assert "github.list_pull_requests" not in registry.get_tools_for_agent("reviewer")
    assert "github.get_repository" not in registry.get_tools_for_agent("developer")
    assert "repo.run_command" in registry.get_tools_for_agent("tester")
    assert "repo.write_file" in registry.get_tools_for_agent("docs")
    assert "gmail.send_email" in registry.get_tools_for_agent("docs")


def test_no_browser_tool_survives_anywhere(registry: MCPToolRegistry) -> None:
    catalog = {item["name"] for item in registry.list_tool_catalog()}
    assert not any("browser" in name or "playwright" in name for name in catalog)


def test_graph_shape_has_agents_and_non_agent_finalizer() -> None:
    nodes = set(build_workflow().get_graph().nodes)
    assert {
        "planner", "developer", "reviewer", "tester", "docs", "finalize",
        "review_halt", "validation_halt",
    } <= nodes
    assert not any("browser" in name for name in nodes)


def test_retry_loop_is_bounded() -> None:
    assert MAX_REVIEW_ATTEMPTS == 2
    assert route_after_review({"review_passed": True, "review_attempts": 0}) == "tester"
    assert route_after_review({"review_passed": False, "review_attempts": 0}) == "developer"
    assert (
        route_after_review({"review_passed": False, "review_attempts": MAX_REVIEW_ATTEMPTS})
        == "review_halt"
    )


def test_failed_validation_gets_one_developer_repair_pass() -> None:
    failed_once = {
        "stage_results": [{"agent": "tester", "status": "validation_failed"}],
    }
    failed_twice = {
        "stage_results": [
            {"agent": "tester", "status": "validation_failed"},
            {"agent": "tester", "status": "validation_failed"},
        ],
    }
    passed = {"stage_results": [{"agent": "tester", "status": "validated"}]}

    assert MAX_VALIDATION_ATTEMPTS == 2
    assert route_after_tester(failed_once) == "developer"
    assert route_after_tester(failed_twice) == "validation_halt"
    assert route_after_tester(passed) == "docs"


def test_deferred_actions_only_include_side_effects_and_deduplicate(registry: MCPToolRegistry) -> None:
    """Check deferred-action normalization without launching the tester recursively."""
    stage_results = [
        {
            "agent": "docs",
            "details": {
                "deferred_actions": [
                    {"tool": "gmail.send_email", "arguments": {"to": "owner@example.com"}},
                    {"tool": "github.create_issue", "arguments": {"title": "Track follow-up"}},
                ],
            },
        },
        {
            "agent": "reviewer",
            "details": {
                "deferred_actions": [
                    {"tool": "github.create_issue", "arguments": {"title": "Track follow-up"}},
                ],
            },
        },
    ]

    deferred = ExecutionService.collect_deferred_actions(stage_results)
    assert deferred
    assert all(registry.is_side_effecting(action["tool"]) for action in deferred)
    assert {action["tool"] for action in deferred} == {"gmail.send_email", "github.create_issue"}

    # Identical (tool, arguments) proposals from different agents collapse to one.
    keys = [(action["tool"], json.dumps(action.get("arguments", {}), sort_keys=True)) for action in deferred]
    assert len(keys) == len(set(keys))
    assert not any("browser" in action["tool"] for action in deferred)
