from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent, last_stage_result
from app.workflow.state import DevTeamState

SYSTEM_PROMPT = (
    "You are the TESTER on a five-agent software engineering team "
    "(planner, developer, reviewer, tester, docs).\n"
    "Your job is to validate the reviewed implementation.\n\n"
    "How to work:\n"
    "- Run exactly this backend validation command once: "
    "python -m pytest backend/tests -q.\n"
    "- Do not run any other commands. Do not use shell syntax, GitHub, or Gmail.\n"
    "- Finish with a short summary of the command result."
)


class TestingAgent(BaseAgent):
    """Runs supported validation commands against the reviewed workspace."""

    name = "tester"

    def run(self, state: DevTeamState) -> dict[str, Any]:
        review = last_stage_result(state, "reviewer") or {}
        checks = [
            "unit tests",
            "integration checks",
            "build validation",
            "smoke validation",
        ]

        changed_files = [
            str(event.get("arguments", {}).get("file_path"))
            for event in state.get("tool_events", [])
            if event.get("tool") == "repo.write_file"
            and event.get("status") == "executed"
            and event.get("arguments", {}).get("file_path")
        ]
        entry, events = self.work(
            status="validated",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=(
                f"Feature request:\n{state.get('request', '')}\n\n"
                f"Reviewer summary:\n{review.get('summary', '(no review output)')}\n\n"
                f"Applied files: {', '.join(dict.fromkeys(changed_files)) or '(none)'}\n\n"
                "Run exactly: python -m pytest backend/tests -q."
            ),
            fallback_calls=[("repo.run_command", {"command": "python -m pytest backend/tests -q"})],
            fallback_summary="Validation suite assembled and executed against the implementation.",
            details={"checks": checks},
        )
        command_events = [event for event in events if event.get("tool") == "repo.run_command"]
        successful_commands = [event for event in command_events if event.get("status") == "executed"]
        failed_commands = [event for event in command_events if event.get("status") in {"error", "blocked"}]
        if failed_commands:
            entry["status"] = "validation_failed"
            entry["summary"] = "One or more validation commands failed or were blocked. See tool activity for details."
        elif successful_commands:
            entry["status"] = "validated"
            entry["summary"] = "Validation command(s) completed successfully in the backend."
        else:
            entry["status"] = "validation_missing"
            entry["summary"] = "The tester did not run a supported validation command."
        return {"current_stage": self.name, "tool_events": events, "stage_results": [entry]}
