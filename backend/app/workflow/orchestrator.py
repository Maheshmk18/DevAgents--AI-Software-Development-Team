from __future__ import annotations

from dataclasses import dataclass
from typing import Any


#: Canonical order of the dev-team stages (browser is not part of this team).
STAGE_ORDER: list[str] = ["planner", "developer", "reviewer", "tester", "docs"]


@dataclass
class WorkflowStage:
    name: str
    agent: str
    description: str
    approval_required: bool = False
    status: str = "pending"


class WorkflowOrchestrator:
    def __init__(self) -> None:
        self.sequence: list[WorkflowStage] = [
            WorkflowStage(
                name="plan",
                agent="planner",
                description="Break the feature request into a clear task plan and dependencies.",
            ),
            WorkflowStage(
                name="implement",
                agent="developer",
                description="Implement the requested work in the appropriate project files.",
                approval_required=True,
            ),
            WorkflowStage(
                name="review",
                agent="reviewer",
                description="Check the implementation for quality, safety, and correctness.",
            ),
            WorkflowStage(
                name="validate",
                agent="tester",
                description="Run the validation, tests, and build checks for the change.",
            ),
            WorkflowStage(
                name="document",
                agent="docs",
                description="Update the relevant documentation for the implemented work.",
            ),
        ]

    def plan(self, request: str) -> dict[str, Any]:
        normalized_request = request.strip()
        tasks = self._llm_tasks(normalized_request) or self._static_tasks()

        return {
            "summary": f"The workflow has been planned for: '{normalized_request[:140]}'.",
            "workflow": [stage.name for stage in self.sequence],
            "tasks": tasks,
        }

    def _llm_tasks(self, request: str) -> list[dict[str, Any]]:
        """Ask the LLM to break the request into concrete engineering tasks.

        Returns an empty list when no LLM is configured or the reply cannot be
        parsed, so :meth:`plan` can fall back to the static plan.
        """
        from app.services.llm_service import llm_service

        system = (
            "You are the PLANNER agent on a software engineering team. Break the "
            "request into 3 to 6 dependency-ordered engineering tasks. Reply with "
            "JSON only, no prose."
        )
        prompt = (
            f"Request: {request}\n\n"
            "Return JSON exactly in this shape:\n"
            '{"tasks":[{"id":"short-id","title":"imperative task",'
            '"owner_agent":"planner|developer|reviewer|tester|docs",'
            '"priority":"low|medium|high","dependencies":["other-id"]}]}'
        )
        payload = llm_service.generate_json(prompt, system)
        if not payload:
            return []

        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, list):
            return []

        tasks: list[dict[str, Any]] = []
        for item in raw_tasks[:6]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            owner = str(item.get("owner_agent", "")).strip()
            if owner not in STAGE_ORDER:
                owner = "developer"
            priority = str(item.get("priority", "")).strip()
            if priority not in ("low", "medium", "high"):
                priority = "medium"
            dependencies = item.get("dependencies")
            tasks.append(
                {
                    "id": str(item.get("id") or f"task-{len(tasks) + 1}"),
                    "title": title,
                    "owner_agent": owner,
                    "priority": priority,
                    "dependencies": (
                        [str(dep) for dep in dependencies if isinstance(dep, (str, int))]
                        if isinstance(dependencies, list)
                        else []
                    ),
                    "status": "pending",
                }
            )
        return tasks

    @staticmethod
    def _static_tasks() -> list[dict[str, Any]]:
        return [
            {
                "id": "plan-task",
                "title": "Plan the request and break it into engineering tasks",
                "owner_agent": "planner",
                "priority": "high",
                "dependencies": [],
                "status": "pending",
            },
            {
                "id": "implement-task",
                "title": "Implement the requested change in code",
                "owner_agent": "developer",
                "priority": "high",
                "dependencies": ["plan-task"],
                "status": "pending",
            },
            {
                "id": "review-task",
                "title": "Review the patch for logic and safety",
                "owner_agent": "reviewer",
                "priority": "high",
                "dependencies": ["implement-task"],
                "status": "pending",
            },
            {
                "id": "validate-task",
                "title": "Run tests and validation for the patch",
                "owner_agent": "tester",
                "priority": "high",
                "dependencies": ["review-task"],
                "status": "pending",
            },
            {
                "id": "doc-task",
                "title": "Update documentation and release notes",
                "owner_agent": "docs",
                "priority": "medium",
                "dependencies": ["validate-task"],
                "status": "pending",
            },
        ]
