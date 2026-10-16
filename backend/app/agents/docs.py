from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent
from app.workflow.state import DevTeamState

SYSTEM_PROMPT = (
    "You are the DOCS agent on a five-agent software engineering team "
    "(planner, developer, reviewer, tester, docs).\n"
    "Your job is to capture the documentation deliverables for the completed work.\n\n"
    "How to work:\n"
    "- Decide the documentation change: a CHANGELOG entry and any README/usage notes.\n"
    "- Call repo.write_file with the COMPLETE new content to propose it.\n"
    "- If a release note or follow-up should be emailed or filed, call "
    "gmail.send_email or github.create_issue to propose it.\n"
    "- All of those tools are side-effecting: they are queued for human approval "
    "and will NOT run during this turn.\n"
    "- Finish with a short summary of the documentation deliverables."
)


class DocumentationAgent(BaseAgent):
    """Captures the documentation deliverables for the completed work."""

    name = "docs"

    def run(self, state: DevTeamState) -> dict[str, Any]:
        request = state.get("request", "")

        entry, events = self.work(
            status="documented",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=(
                f"Feature request:\n{request}\n\n"
                "Produce the documentation update for this work."
            ),
            fallback_calls=[
                (
                    "repo.write_file",
                    {"file_path": "CHANGELOG.md", "content": self._changelog_entry(request)},
                )
            ],
            fallback_summary="Documentation updated for the completed work.",
        )
        return {"current_stage": self.name, "tool_events": events, "stage_results": [entry]}

    @staticmethod
    def _changelog_entry(request: str) -> str:
        return "\n".join(
            [
                "# Changelog",
                "",
                "## Unreleased",
                f"- {request.strip()}",
                "",
            ]
        )
