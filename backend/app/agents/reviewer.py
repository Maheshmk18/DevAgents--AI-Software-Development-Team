from __future__ import annotations

import re
from typing import Any

from app.agents.base import BaseAgent, extract_path_tokens, last_stage_result
from app.agents.runtime import AgentRuntime
from app.workflow.state import DevTeamState

#: The reviewer ends its message with a machine-readable verdict line, which the
#: graph uses to decide whether to loop back to the developer.
_VERDICT_RE = re.compile(r"VERDICT\s*[:\-]\s*(APPROVED|CHANGES[_ ]REQUESTED)", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You are the REVIEWER on a five-agent software engineering team "
    "(planner, developer, reviewer, tester, docs).\n"
    "Your job is the quality and safety gate for the developer's work.\n\n"
    "How to work:\n"
    "- Review only the changed files using repo.read_file. The tool results are "
    "the source of truth for the workspace.\n"
    "- Read only the additional files needed to verify imports and route wiring. "
    "Do not inspect conftest.py unless the changed test uses a shared fixture. "
    "Do not search or list unrelated repository files.\n"
    "- For FastAPI endpoint changes, trace the router prefix and app registration to "
    "confirm the exact requested URL is reachable. Do not approve a route that is "
    "defined in an unregistered router or that the endpoint test would return 404 for.\n"
    "- Do not run commands or inspect GitHub. The tester handles validation.\n"
    "- Judge correctness, scope, and risk. Be specific about problems.\n"
    "- End your reply with exactly one verdict line:\n"
    "  VERDICT: APPROVED\n"
    "  or\n"
    "  VERDICT: CHANGES_REQUESTED"
)


class ReviewAgent(BaseAgent):
    """Reviews the implementation and decides whether it may proceed."""

    name = "reviewer"

    def run(self, state: DevTeamState) -> dict[str, Any]:
        attempts = state.get("review_attempts", 0) + 1
        developer = last_stage_result(state, "developer")
        deterministic_pass = bool(developer) and developer.get("status") == "implementation_ready"

        changed_files = [
            str(event.get("arguments", {}).get("file_path"))
            for event in state.get("tool_events", [])
            if event.get("tool") == "repo.write_file"
            and event.get("status") == "executed"
            and event.get("arguments", {}).get("file_path")
        ]
        # When the developer has written files, review those exact paths. A
        # filename merely mentioned in the request may not exist and should not
        # make an otherwise valid review fail.
        target_files = list(dict.fromkeys(changed_files or extract_path_tokens(state.get("request", ""))))
        fallback_calls: list[tuple[str, dict[str, Any]]] = [
            ("repo.read_file", {"file_path": path}) for path in target_files
        ]
        user_prompt = (
            f"Feature request:\n{state.get('request', '')}\n\n"
            f"Developer summary:\n{(developer or {}).get('summary', '(no developer output)')}\n\n"
            f"Files changed in the workspace: {', '.join(dict.fromkeys(changed_files)) or '(none)'}\n\n"
            f"Files named in the request: {', '.join(target_files) or '(none)'}\n\n"
            "Review the implementation and give your verdict."
        )

        runtime = AgentRuntime(self.name, self.registry)
        turn = runtime.run(SYSTEM_PROMPT, user_prompt, max_steps=3)

        if turn is None:
            events, deferred = self.invoke_tools(fallback_calls)
            validation_errors = self._unreadable_files(events)
            passed = deterministic_pass and not validation_errors
            summary = (
                "Review passed: the implementation is ready for validation."
                if passed
                else (
                    "Review could not pass because a changed file could not be read."
                    if validation_errors
                    else "Review requested changes to the implementation."
                )
            )
            engine = "deterministic"
        else:
            events, deferred = turn["events"], turn["deferred"]
            summary = turn["summary"] or (
                "Review passed: the implementation is ready for validation."
                if deterministic_pass
                else "Review requested changes to the implementation."
            )
            passed = self._verdict(summary, default=deterministic_pass)
            validation_errors = self._unreadable_files(events)
            if validation_errors:
                # Retry the actual changed files directly. The model may have
                # hit its tool-call cap or tried a stale path; neither means
                # the developer's files are missing from the workspace.
                retry_events, _ = self.invoke_tools(fallback_calls)
                events.extend(retry_events)
                validation_errors = self._unreadable_files(retry_events)
                if validation_errors:
                    passed = False
                    summary = f"{summary}\nReview blocked because a changed file could not be read after retry."
            engine = "llm"

        entry = self.stage_entry(
            status="review_passed" if passed else "changes_requested",
            summary=summary,
            details={
                "attempt": attempts,
                "engine": engine,
                "verdict": "approved" if passed else "changes_requested",
                "deferred_actions": deferred,
            },
        )
        return {
            "current_stage": self.name,
            "tool_events": events,
            "stage_results": [entry],
            "review_passed": passed,
            "review_attempts": attempts,
        }

    @staticmethod
    def _verdict(text: str, default: bool) -> bool:
        match = _VERDICT_RE.search(text or "")
        if not match:
            return default
        verdict = match.group(1).upper().replace(" ", "_")
        return verdict != "CHANGES_REQUESTED"

    @staticmethod
    def _unreadable_files(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return failed reads and empty results for the paths under review."""
        return [
            event
            for event in events
            if event.get("tool") == "repo.read_file"
            and (
                event.get("status") in {"error", "blocked"}
                or not str(event.get("result") or "").strip()
            )
        ]
