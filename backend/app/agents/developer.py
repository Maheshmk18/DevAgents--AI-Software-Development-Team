from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent, extract_path_tokens, last_stage_result
from app.workflow.state import DevTeamState

SYSTEM_PROMPT = (
    "You are the DEVELOPER on a five-agent software engineering team "
    "(planner, developer, reviewer, tester, docs).\n"
    "Your job is to turn the plan into a concrete, reviewable implementation.\n\n"
    "How to work:\n"
    "- Read the files named in the request with repo.read_file before changing them.\n"
    "- Decide the real change and write out the COMPLETE new file content.\n"
    "- Call repo.write_file with file_path and content to propose each file change.\n"
    "- Call repo.run_command with the exact test/build command to prove the change works.\n"
    "- repo.write_file and repo.run_command are side-effecting: they are queued for "
    "human approval and will NOT run during this turn. Propose them anyway.\n"
    "- Finish with a short summary: what you changed, in which files, and why it is safe."
)


class DeveloperAgent(BaseAgent):
    """Produces the implementation and proposes the file writes / test command."""

    name = "developer"

    def run(self, state: DevTeamState) -> dict[str, Any]:
        request = state.get("request", "")
        plan = state.get("plan", [])
        attempt = state.get("review_attempts", 0) + 1

        target_files = extract_path_tokens(request)
        review_note = ""
        if attempt > 1:
            review = last_stage_result(state, "reviewer")
            if review:
                review_note = (
                    "\nA previous attempt was sent back by the reviewer. "
                    f"Address this feedback:\n{review.get('summary', '')}\n"
                )

        plan_lines = "\n".join(
            f"- {task.get('title', '')}" for task in plan if isinstance(task, dict)
        )
        user_prompt = (
            f"Feature request:\n{request}\n\n"
            f"Planned tasks:\n{plan_lines or '(none)'}\n"
            f"Files named in the request: {', '.join(target_files) or '(none)'}\n"
            f"{review_note}\n"
            "Implement the change now."
        )

        fallback_calls: list[tuple[str, dict[str, Any]]] = [
            ("repo.read_file", {"file_path": path}) for path in target_files
        ]
        fallback_calls.append(
            (
                "repo.write_file",
                {"file_path": "IMPLEMENTATION_NOTES.md", "content": self._implementation_notes(request, plan, attempt)},
            )
        )
        fallback_calls.append(("repo.run_command", {"command": "python -m pytest -q"}))

        entry, events = self.work(
            status="implementation_ready",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            fallback_calls=fallback_calls,
            fallback_summary=f"Prepared an implementation for {len(plan)} planned task(s) (attempt {attempt}).",
            details={
                "attempt": attempt,
                "tasks_planned": len(plan),
                "target_files": target_files,
            },
        )
        return {"current_stage": self.name, "tool_events": events, "stage_results": [entry]}

    @staticmethod
    def _implementation_notes(request: str, plan: list[dict[str, Any]], attempt: int) -> str:
        lines = [
            "# Implementation Notes",
            "",
            f"Request: {request.strip()}",
            f"Attempt: {attempt}",
            "",
            "## Planned tasks",
        ]
        lines.extend(f"- {task.get('id', '?')}: {task.get('title', '')}" for task in plan)
        lines.extend(
            [
                "",
                "## Notes",
                "- Confirm the change is scoped to the interfaces touched above.",
                "- Run the validation suite before opening a pull request.",
                "",
            ]
        )
        return "\n".join(lines)
