from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.core.util import truncate
from app.agents.runtime import AgentRuntime
from app.mcp.tool_registry import MCPToolRegistry
from app.workflow.state import DevTeamState

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

    Tool policy: an agent only auto-runs tools that are **allowed for its role**
    *and* marked read-only. Side-effecting tools (writes, shell commands,
    outbound email, issue creation) are returned as ``deferred`` proposals so a
    human can approve them later — they are never executed automatically.
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
        """Run allowed read-only tools.

        Returns ``(events, deferred)`` where ``events`` records executed
        read-only calls and ``deferred`` holds side-effecting proposals that
        carry the exact arguments a human would approve.
        """
        allowed = set(self.registry.get_tools_for_agent(self.name))
        events: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []

        for tool_name, arguments in calls:
            if tool_name not in allowed:
                continue
            if not self.registry.is_read_only(tool_name):
                deferred.append({"agent": self.name, "tool": tool_name, "arguments": arguments})
                continue
            try:
                meta = self.registry.call_tool_with_meta(tool_name, arguments)
                event: dict[str, Any] = {
                    "agent": self.name,
                    "tool": tool_name,
                    "status": "ok",
                    "via": meta.get("via"),
                    "result": truncate(meta.get("result")),
                }
                if meta.get("mcp_error"):
                    event["mcp_error"] = meta["mcp_error"]
                events.append(event)
            except Exception as exc:  # never let a tool failure break the run
                events.append(
                    {"agent": self.name, "tool": tool_name, "status": "error", "error": str(exc)}
                )
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
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run the agent's LLM tool-calling loop, or fall back deterministically.

        When an LLM is configured the model decides which of the agent's tools
        to call (read-only calls run immediately, side-effecting ones become
        deferred proposals). With no LLM the agent still does something useful
        via ``fallback_calls`` so the workflow never dead-ends offline.

        Returns ``(stage_entry, tool_events)``.
        """
        runtime = AgentRuntime(self.name, self.registry)
        turn = runtime.run(system_prompt, user_prompt)

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
