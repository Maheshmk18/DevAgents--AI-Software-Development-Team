from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.core.util import truncate
from app.mcp.tool_registry import MCPToolRegistry
from app.services.llm_service import llm_service
from app.workflow.graph import get_workflow
from app.workflow.orchestrator import STAGE_ORDER


class ExecutionService:
    """Runs the dev-team LangGraph workflow and normalises its result."""

    def __init__(self) -> None:
        self.stage_order = list(STAGE_ORDER)
        self.registry = MCPToolRegistry()

    def execute_run(self, run: dict[str, Any]) -> dict[str, Any]:
        run_id = str(run.get("id", ""))
        request = str(run.get("message", ""))

        initial_state: dict[str, Any] = {
            "run_id": run_id,
            "request": request,
            "plan": run.get("tasks") or [],
            "stage_results": [],
            "tool_events": [],
            "review_attempts": 0,
            "review_passed": True,
        }

        final_state = get_workflow().invoke(initial_state)
        stage_results = list(final_state.get("stage_results", []))
        tool_events = list(final_state.get("tool_events", []))
        deferred_actions = self.collect_deferred_actions(stage_results)
        final_answer = final_state.get("final_answer") or llm_service.summarize_run(
            request, stage_results
        )

        return {
            "run_id": run_id,
            "status": final_state.get("status", "completed"),
            "stage_results": stage_results,
            "tool_events": tool_events,
            "deferred_actions": deferred_actions,
            "final_answer": final_answer,
            "updated_at": datetime.now(timezone.utc),
        }

    @staticmethod
    def collect_deferred_actions(stage_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Gather unique side-effecting proposals from every stage."""
        seen: set[tuple[str, str]] = set()
        actions: list[dict[str, Any]] = []
        for entry in stage_results:
            for action in entry.get("details", {}).get("deferred_actions", []) or []:
                if not isinstance(action, dict) or not action.get("tool"):
                    continue
                key = (
                    action["tool"],
                    json.dumps(action.get("arguments", {}), sort_keys=True, default=str),
                )
                if key in seen:
                    continue
                seen.add(key)
                actions.append(action)
        return actions

    def execute_deferred(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Execute approved side-effecting actions and record the outcome."""
        results: list[dict[str, Any]] = []
        for action in actions:
            tool_name = str(action.get("tool", ""))
            arguments = action.get("arguments") or {}
            try:
                meta = self.registry.call_tool_with_meta(tool_name, arguments)
                results.append(
                    {
                        "agent": action.get("agent"),
                        "tool": tool_name,
                        "status": "executed",
                        "via": meta.get("via"),
                        "server": meta.get("server"),
                        "result": truncate(meta.get("result")),
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "agent": action.get("agent"),
                        "tool": tool_name,
                        "status": "error",
                        "error": str(exc),
                    }
                )
        return results
