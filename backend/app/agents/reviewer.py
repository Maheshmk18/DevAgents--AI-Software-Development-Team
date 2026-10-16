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
    "- Re-read the touched files with repo.read_file and inspect open pull "
    "requests with github.list_pull_requests when useful.\n"
    "- You may propose repo.run_command to compile or lint; side-effecting tools "
    "are queued for human approval and will NOT run now.\n"
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

        target_files = extract_path_tokens(state.get("request", ""))
        fallback_calls: list[tuple[str, dict[str, Any]]] = [
            ("repo.read_file", {"file_path": path}) for path in target_files
        ]
        fallback_calls.append(("repo.run_command", {"command": "python -m compileall -q ."}))

        user_prompt = (
            f"Feature request:\n{state.get('request', '')}\n\n"
            f"Developer summary:\n{(developer or {}).get('summary', '(no developer output)')}\n\n"
            f"Files named in the request: {', '.join(target_files) or '(none)'}\n\n"
            "Review the implementation and give your verdict."
        )

        runtime = AgentRuntime(self.name, self.registry)
        turn = runtime.run(SYSTEM_PROMPT, user_prompt)

        if turn is None:
            events, deferred = self.invoke_tools(fallback_calls)
            passed = deterministic_pass
            summary = (
                "Review passed: the implementation is ready for validation."
                if passed
                else "Review requested changes to the implementation."
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
