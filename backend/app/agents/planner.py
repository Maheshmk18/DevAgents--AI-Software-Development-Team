from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent
from app.workflow.orchestrator import WorkflowOrchestrator
from app.workflow.state import DevTeamState

SYSTEM_PROMPT = (
    "You are the PLANNER on a five-agent software engineering team "
    "(planner, developer, reviewer, tester, docs).\n"
    "Your job is to break the user's request into a dependency-ordered "
    "engineering plan grounded in the real repository.\n\n"
    "How to work:\n"
    "- Inspect with at most one root listing, two focused repo.list_files calls, "
    "and two repo.search_files calls (five calls total).\n"
    "- If you have already listed the repository root, list a subdirectory only "
    "when the root listing did not show the relevant files.\n"
    "- Use what you find to make the plan concrete (name real files and modules).\n"
    "- Never claim to have changed anything; you only plan.\n"
    "- Finish with a concise plan: the ordered tasks, who owns each one, and the "
    "main risks or unknowns."
)


class PlannerAgent(BaseAgent):
    """Plans the request and grounds the plan in a repository snapshot."""

    name = "planner"

    def run(self, state: DevTeamState) -> dict[str, Any]:
        request = state.get("request", "")
        plan = state.get("plan") or WorkflowOrchestrator().plan(request).get("tasks", [])
        task_lines = "\n".join(
            f"- {task.get('id', '?')} (owner: {task.get('owner_agent', '?')}): {task.get('title', '')}"
            for task in plan
        )

        entry, events = self.work(
            status="planned",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=(
                f"Feature request:\n{request}\n\n"
                f"Draft task list to refine and order:\n{task_lines or '(none)'}\n\n"
                "Inspect the repository, then produce the final plan."
            ),
            fallback_calls=[("repo.list_files", {"relative_path": ".", "max_files": 60})],
            fallback_summary=f"The workflow has been planned for: '{request.strip()[:140]}'.",
            details={"tasks_planned": len(plan)},
        )
        return {"current_stage": self.name, "tool_events": events, "stage_results": [entry]}
