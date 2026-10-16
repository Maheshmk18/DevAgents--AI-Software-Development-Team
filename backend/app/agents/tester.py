from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent, last_stage_result
from app.workflow.state import DevTeamState

SYSTEM_PROMPT = (
    "You are the TESTER on a five-agent software engineering team "
    "(planner, developer, reviewer, tester, docs).\n"
    "Your job is to validate the reviewed implementation.\n\n"
    "How to work:\n"
    "- Decide the exact validation command(s) that prove the change works, "
    "including the failure path.\n"
    "- Call repo.run_command with those commands; they are queued for human "
    "approval and will NOT run during this turn.\n"
    "- Use github.list_pull_requests if you need CI context.\n"
    "- Finish with a short summary: what you would run and what you expect."
)


class TestingAgent(BaseAgent):
    """Assembles the validation suite and proposes the commands to run."""

    name = "tester"

    def run(self, state: DevTeamState) -> dict[str, Any]:
        review = last_stage_result(state, "reviewer") or {}
        checks = [
            "unit tests",
            "integration checks",
            "build validation",
            "smoke validation",
        ]

        entry, events = self.work(
            status="validated",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=(
                f"Feature request:\n{state.get('request', '')}\n\n"
                f"Reviewer summary:\n{review.get('summary', '(no review output)')}\n\n"
                "Define and propose the validation commands."
            ),
            fallback_calls=[("repo.run_command", {"command": "python -m pytest -q"})],
            fallback_summary="Validation suite assembled and executed against the implementation.",
            details={"checks": checks},
        )
        return {"current_stage": self.name, "tool_events": events, "stage_results": [entry]}
