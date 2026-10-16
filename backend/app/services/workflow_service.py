from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.db import (
    get_approval as db_get_approval,
    get_evaluation as db_get_evaluation,
    get_run as db_get_run,
    insert_approval,
    insert_run,
    list_approvals as db_list_approvals,
    list_evaluations as db_list_evaluations,
    list_runs as db_list_runs,
    update_approval as db_update_approval,
)
from app.mcp.tool_registry import MCPToolRegistry
from app.services.approval_service import ApprovalService
from app.services.evaluator_service import EvaluatorService
from app.services.execution_service import ExecutionService
from app.workflow.orchestrator import WorkflowOrchestrator


class WorkflowService:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.evaluations: dict[str, dict[str, Any]] = {}
        self.tool_registry = MCPToolRegistry()
        self.approval_service = ApprovalService()
        self.evaluator_service = EvaluatorService()
        self.execution_service = ExecutionService()

    def create_run(self, user_message: str) -> dict[str, Any]:
        run_id = uuid4().hex
        now = datetime.now(timezone.utc)
        run_data = {
            "id": run_id,
            "message": user_message,
            "status": "waiting_for_human_approval",
            "created_at": now,
            "updated_at": now,
            "tasks": [],
            "approvals": [],
            "stage_results": [],
            "tool_events": [],
            "deferred_actions": [],
            "action_results": [],
            "final_answer": None,
            "error": None,
        }
        self.runs[run_id] = run_data

        orchestrator = WorkflowOrchestrator()
        workflow_result = orchestrator.plan(user_message)
        run_data["tasks"] = workflow_result.get("tasks", [])
        run_data["updated_at"] = datetime.now(timezone.utc)
        run_data["tool_access"] = {
            "planner": self.tool_registry.get_tools_for_agent("planner"),
            "developer": self.tool_registry.get_tools_for_agent("developer"),
            "reviewer": self.tool_registry.get_tools_for_agent("reviewer"),
            "tester": self.tool_registry.get_tools_for_agent("tester"),
            "docs": self.tool_registry.get_tools_for_agent("docs"),
        }

        approval = self.create_approval(
            run_id,
            "approve_plan_before_execution",
            {
                "stage": "planning",
                "notes": "Review the generated engineering plan before the team begins implementation.",
            },
        )
        run_data["approvals"] = [approval["id"]]
        insert_run(run_data)
        return run_data

    def list_runs(self) -> list[dict[str, Any]]:
        mongo_runs = db_list_runs()
        if mongo_runs:
            return [self._serialize_run(run) for run in mongo_runs]
        return list(self.runs.values())

    def list_approvals(self) -> list[dict[str, Any]]:
        mongo_approvals = db_list_approvals()
        if mongo_approvals:
            return [self._serialize_approval(approval) for approval in mongo_approvals]
        return [self._serialize_approval(approval) for approval in self.approvals.values()]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        mongo_run = db_get_run(run_id)
        if mongo_run:
            return mongo_run
        return self.runs.get(run_id)

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        mongo_approval = db_get_approval(approval_id)
        if mongo_approval:
            return mongo_approval
        return self.approvals.get(approval_id)

    def evaluate_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError("Run not found")
        result = self.evaluator_service.evaluate_run(run)
        self.evaluations[result["run_id"]] = result
        return result

    def list_evaluations(self) -> list[dict[str, Any]]:
        mongo_evaluations = db_list_evaluations()
        if mongo_evaluations:
            return mongo_evaluations
        return list(self.evaluations.values())

    def execute_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError("Run not found")
        execution_result = self.execution_service.execute_run(run)
        run["final_answer"] = execution_result["final_answer"]
        run["updated_at"] = execution_result["updated_at"]
        run["stage_results"] = execution_result["stage_results"]
        run["tool_events"] = execution_result.get("tool_events", [])

        deferred_actions = execution_result.get("deferred_actions", [])
        run["deferred_actions"] = deferred_actions
        run["action_results"] = []

        if deferred_actions:
            action_approval = self.create_approval(
                run_id,
                "execute_deferred_actions",
                {
                    "stage": "implementation",
                    "notes": "Approve to execute the side-effecting actions the team proposed.",
                    "actions": deferred_actions,
                },
            )
            run["status"] = "waiting_for_action_approval"
            run["action_approval"] = action_approval["id"]
            run["approvals"] = [*run.get("approvals", []), action_approval["id"]]
        else:
            run["status"] = execution_result["status"]

        self.runs[run_id] = run
        insert_run(run)
        return execution_result

    def create_approval(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        record = self.approval_service.create_approval(run_id, action, payload)
        self.approvals[record["id"]] = record
        insert_approval(record)
        return record

    def update_approval(self, approval_id: str, decision: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        approval = self.approvals.get(approval_id)
        if approval is None:
            approval = db_get_approval(approval_id)
        if approval is None:
            raise KeyError("Approval not found")
        approval = self.approval_service.update_approval(approval_id, decision, arguments)
        self.approvals[approval_id] = approval
        db_update_approval(approval_id, approval)

        run = self.runs.get(approval["run_id"])
        if run is None:
            run = db_get_run(approval["run_id"])
        if run is None:
            return approval

        decided_at = approval.get("updated_at") or datetime.now(timezone.utc)
        run["updated_at"] = decided_at

        # Gate 2: execute (or skip) the side-effecting actions the team proposed.
        if approval.get("action") == "execute_deferred_actions":
            deferred = run.get("deferred_actions", [])
            if decision == "approve":
                run["action_results"] = self.execution_service.execute_deferred(deferred)
            else:
                run["action_results"] = [
                    {"agent": item.get("agent"), "tool": item.get("tool"), "status": "skipped"}
                    for item in deferred
                ]
            run["status"] = "completed"
            self.runs[approval["run_id"]] = run
            insert_run(run)
            return approval

        # Gate 1: approve or reject the plan before the team starts working.
        if decision == "approve":
            run["status"] = "approved"
            run["approvals"] = [approval_id]
            self.runs[approval["run_id"]] = run
            insert_run(run)
            self.execute_run(approval["run_id"])
        else:
            run["status"] = "rejected"
            run["approvals"] = [approval_id]
            self.runs[approval["run_id"]] = run
            insert_run(run)

        return approval

    def _serialize_run(self, run: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": run.get("id"),
            "message": run.get("message"),
            "status": run.get("status"),
            "created_at": run.get("created_at"),
            "updated_at": run.get("updated_at"),
            "tasks": run.get("tasks", []),
            "approvals": run.get("approvals", []),
            "final_answer": run.get("final_answer"),
            "error": run.get("error"),
            "tool_access": run.get("tool_access", {}),
            "stage_results": run.get("stage_results", []),
            "tool_events": run.get("tool_events", []),
            "deferred_actions": run.get("deferred_actions", []),
            "action_results": run.get("action_results", []),
            "action_approval": run.get("action_approval"),
        }

    def _serialize_approval(self, approval: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": approval.get("id"),
            "run_id": approval.get("run_id"),
            "action": approval.get("action"),
            "status": approval.get("status"),
            "payload": approval.get("payload", {}),
            "created_at": approval.get("created_at"),
            "updated_at": approval.get("updated_at"),
            "arguments": approval.get("arguments", {}),
        }


workflow_service = WorkflowService()

