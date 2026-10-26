from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.core.util import truncate
from app.agents.runtime import AgentRuntime, publish_tool_event
from app.mcp.tool_registry import MCPToolRegistry
from app.workflow.state import DevTeamState

# Role-specific caps keep agents focused while leaving enough turns for the
# normal inspect, edit, and response cycle.
AGENT_TOOL_STEP_BUDGETS = {
    "planner": 4,
    "developer": 6,
    "reviewer": 3,
    "tester": 2,
    "docs": 3,
}

#: Matches file-like tokens in a request, e.g. "app/main.py" or "src/App.tsx".
_PATH_PATTERN = re.compile(
    r"[\w./\\-]+\.(?:py|tsx|ts|jsx|js|json|md|txt|toml|ya?ml|css|html)"
)


def extract_path_tokens(text: str, limit: int = 3) -> list[str]:
    """Pull file-path-looking tokens out of a free-text request."""
    found: list[str] = []
    for match in _PATH_PATTERN.findall(text or ""):
        token = match.strip("`'\" ")
        if token and token not in found:
            found.append(token)
        if len(found) >= limit:
            break
    return found


def last_stage_result(state: DevTeamState, agent_name: str) -> dict[str, Any] | None:
    """Return the most recent stage entry produced by ``agent_name``."""
    for entry in reversed(state.get("stage_results", [])):
        if entry.get("agent") == agent_name:
            return entry
    return None


class BaseAgent:
    """Base class for every dev-team agent.

    An agent receives the shared graph state and returns a partial state
    update. Every agent must contribute exactly one ``stage_results`` entry so
    the run history stays reviewable.

    Tool policy: workspace reads/writes and allowlisted validation run in the
    backend. External GitHub and Gmail actions wait for the final human gate.
    """

    name: str = "base"

    def __init__(self) -> None:
        self.registry = MCPToolRegistry()

    def run(self, state: DevTeamState) -> dict[str, Any]:
        raise NotImplementedError("Agent implementations must provide a run() method.")

    def invoke_tools(
        self,
        calls: list[tuple[str, dict[str, Any]]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Run workspace-safe tools and defer external actions."""
        allowed = set(self.registry.get_tools_for_agent(self.name))
        events: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []

        for tool_name, arguments in calls:
            if tool_name not in allowed:
                continue
            event_id = uuid4().hex
            if self.registry.requires_human_approval(tool_name):
                deferred.append({
                    "event_id": event_id,
                    "agent": self.name,
                    "tool": tool_name,
                    "arguments": arguments,
                })
                event = {
                    "event_id": event_id,
                    "agent": self.name,
                    "tool": tool_name,
                    "status": "awaiting_approval",
                    "arguments": arguments,
                }
                events.append(event)
                publish_tool_event(event)
                continue
            if not self.registry.can_run_automatically(tool_name, arguments):
                event = {
                    "event_id": event_id,
                    "agent": self.name,
                    "tool": tool_name,
                    "status": "blocked",
                    "arguments": arguments,
                    "error": "This tool call is outside the workspace or validation allowlist.",
                }
                events.append(event)
                publish_tool_event(event)
                continue
            publish_tool_event({
                "event_id": event_id,
                "agent": self.name,
                "tool": tool_name,
                "status": "running",
                "arguments": arguments,
            })
            try:
                meta = self.registry.call_tool_with_meta(tool_name, arguments)
                result = meta.get("result")
                command_failed = (
                    tool_name == "repo.run_command"
                    and isinstance(result, dict)
                    and result.get("return_code") != 0
                )
                event: dict[str, Any] = {
                    "event_id": event_id,
                    "agent": self.name,
                    "tool": tool_name,
                    "status": "error" if command_failed else (
                        "executed" if tool_name in {"repo.write_file", "repo.run_command"} else "ok"
                    ),
                    "arguments": arguments,
                    "via": meta.get("via"),
                    "result": truncate(result),
                }
                if command_failed:
                    event["error"] = str(
                        result.get("diagnostic")
                        or result.get("stderr")
                        or result.get("stdout")
                        or "Validation command failed."
                    )[:1200]
                if meta.get("mcp_error"):
                    event["mcp_error"] = meta["mcp_error"]
                events.append(event)
                publish_tool_event(event)
            except Exception as exc:  # never let a tool failure break the run
                event = {
                    "event_id": event_id,
                    "agent": self.name,
                    "tool": tool_name,
                    "status": "error",
                    "arguments": arguments,
                    "error": str(exc),
                }
                events.append(event)
                publish_tool_event(event)
        return events, deferred

    def stage_entry(
        self,
        *,
        status: str,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "agent": self.name,
            "status": status,
            "summary": summary,
            "details": details or {},
            "updated_at": datetime.now(timezone.utc),
        }

    def work(
        self,
        *,
        status: str,
        system_prompt: str,
        user_prompt: str,
        fallback_calls: list[tuple[str, dict[str, Any]]],
        fallback_summary: str,
        details: dict[str, Any] | None = None,
        force_initial_tool_call: bool = False,
        tool_allowlist: list[str] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run the agent's LLM tool-calling loop, or fall back deterministically.

        When an LLM is configured the model decides which of the agent's tools
        to call (workspace-safe calls run immediately, external actions are
        deferred). With no LLM the agent still does something useful
        via ``fallback_calls`` so the workflow never dead-ends offline.

        Returns ``(stage_entry, tool_events)``.
        """
        runtime = AgentRuntime(self.name, self.registry)
        turn = runtime.run(
            system_prompt,
            user_prompt,
            max_steps=AGENT_TOOL_STEP_BUDGETS.get(self.name, 4),
            force_initial_tool_call=force_initial_tool_call,
            tool_allowlist=tool_allowlist,
        )

        merged: dict[str, Any] = dict(details or {})
        if turn is None:
            events, deferred = self.invoke_tools(fallback_calls)
            summary = fallback_summary
            merged["engine"] = "deterministic"
        else:
            events, deferred = turn["events"], turn["deferred"]
            summary = turn["summary"] or fallback_summary
            merged["engine"] = "llm"
            merged["llm_steps"] = turn["steps"]

        merged["deferred_actions"] = deferred
        entry = self.stage_entry(status=status, summary=summary, details=merged)
        return entry, events
