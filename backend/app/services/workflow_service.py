from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.db import (
    delete_all_runs,
    delete_run,
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
from app.mcp.tool_registry import MCPToolRegistry, default_repo_parts
from app.services.approval_service import ApprovalService
from app.services.evaluator_service import EvaluatorService
from app.services.execution_service import ExecutionService
from app.services.agent_run_context import agent_run_context
from app.services.github_workspace import GitHubWorkspaceService
from app.services.pdf_documentation import write_run_documentation_pdf
from app.workflow.orchestrator import WorkflowOrchestrator


_CONVERSATIONAL_REPLIES = {
    "hi": "Hi! Describe a feature, bug, or code change you want the engineering team to work on.",
    "hello": "Hello! Describe a feature, bug, or code change you want the engineering team to work on.",
    "hey": "Hey! Describe a feature, bug, or code change you want the engineering team to work on.",
    "hi there": "Hi! Describe a feature, bug, or code change you want the engineering team to work on.",
    "hello there": "Hello! Describe a feature, bug, or code change you want the engineering team to work on.",
    "hey there": "Hey! Describe a feature, bug, or code change you want the engineering team to work on.",
    "good morning": "Good morning! Describe a feature, bug, or code change you want the engineering team to work on.",
    "good afternoon": "Good afternoon! Describe a feature, bug, or code change you want the engineering team to work on.",
    "good evening": "Good evening! Describe a feature, bug, or code change you want the engineering team to work on.",
    "thanks": "You're welcome. Send a feature, bug, or code change whenever you're ready.",
    "thank you": "You're welcome. Send a feature, bug, or code change whenever you're ready.",
    "what can you do": "I can help plan and implement software changes, review them, run supported checks, update documentation, and prepare approved GitHub or Gmail actions.",
}


def _conversational_reply(message: str) -> str | None:
    """Keep greetings and simple capability questions out of the code workflow."""
    normalized = " ".join(re.findall(r"[a-z]+", str(message or "").casefold()))
    return _CONVERSATIONAL_REPLIES.get(normalized)


def _action_failure_message(failed_actions: list[dict[str, Any]]) -> str:
    details: list[str] = []
    for result in failed_actions:
        tool = str(result.get("tool") or "Approved action")
        reason = str(result.get("error") or "The action did not complete.").strip()
        details.append(f"{tool}: {reason[:280]}")
    if not details:
        return "Delivery finished with issues. Review the tool call details."
    return "Delivery finished with issues: " + " ".join(details[:3])


def _dedupe(values: list[Any]) -> list[Any]:
    """Order-preserving de-duplication (approval ids accumulate across gates)."""
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


class WorkflowService:
    #: Statuses that mean "the team is still working" — the console polls while
    #: a run is in one of these, then stops once it settles on a final state.
    ACTIVE_STATUSES = ("approved", "running")

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.evaluations: dict[str, dict[str, Any]] = {}
        self.tool_registry = MCPToolRegistry()
        self.approval_service = ApprovalService()
        self.evaluator_service = EvaluatorService()
        self.execution_service = ExecutionService()
        #: Runs whose background thread is live, so a double-click cannot start
        #: the same graph twice.
        self._executing: set[str] = set()
        self._approval_lock = threading.Lock()

    def create_run(self, user_message: str) -> dict[str, Any]:
        run_id = uuid4().hex
        now = datetime.now(timezone.utc)
        conversational_reply = _conversational_reply(user_message)
        run_data = {
            "id": run_id,
            "message": user_message,
            "status": "completed" if conversational_reply else "running",
            "current_stage": None if conversational_reply else "planning",
            "created_at": now,
            "updated_at": now,
            "tasks": [],
            "approvals": [],
            "stage_results": [],
            "tool_events": [],
            "deferred_actions": [],
            "action_results": [],
            "final_answer": conversational_reply,
            "error": None,
            "plan_summary": None,
            "repository_slug": None,
            "workspace_root": None,
            "branch_name": None,
            "base_branch": None,
            "workspace_review": None,
            "pull_request_url": None,
        }
        self.runs[run_id] = run_data
        if conversational_reply:
            # A greeting or capability question is not a repository change:
            # don't call the LLM, create approval gates, or queue external work.
            insert_run(run_data)
            return run_data
        run_data["tool_access"] = {
            "planner": self.tool_registry.get_tools_for_agent("planner"),
            "developer": self.tool_registry.get_tools_for_agent("developer"),
            "reviewer": self.tool_registry.get_tools_for_agent("reviewer"),
            "tester": self.tool_registry.get_tools_for_agent("tester"),
            "docs": self.tool_registry.get_tools_for_agent("docs"),
        }
        insert_run(run_data)
        self._start_plan(run_id)
        return run_data

    def _start_plan(self, run_id: str) -> None:
        if run_id in self._executing:
            return
        self._executing.add(run_id)
        thread = threading.Thread(
            target=self._prepare_plan_in_background,
            args=(run_id,),
            name=f"devteam-plan-{run_id[:8]}",
            daemon=True,
        )
        thread.start()

    def _prepare_plan_in_background(self, run_id: str) -> None:
        try:
            run = self.get_run(run_id)
            if run is None:
                return
            plan = WorkflowOrchestrator().plan(str(run.get("message", "")))
            owner, repo = default_repo_parts()
            repository_slug = f"{owner}/{repo}" if owner and repo else ""
            run["tasks"] = plan.get("tasks", [])
            run["plan_summary"] = plan.get("summary") or "The engineering team prepared this plan for your request."
            run["repository_slug"] = repository_slug or None
            run["status"] = "waiting_for_plan_approval"
            run["current_stage"] = None
            run["updated_at"] = datetime.now(timezone.utc)
            approval = self.create_approval(
                run_id,
                "approve_plan_before_execution",
                {
                    "stage": "plan_review",
                    "summary": run["plan_summary"],
                    "tasks": run["tasks"],
                    "repository_slug": repository_slug or None,
                    "notes": (
                        f"The plan is ready for {repository_slug}. Approving creates an isolated branch and starts implementation."
                        if repository_slug
                        else "The plan is ready. Connect GitHub and select a repository in Integrations before approving implementation."
                    ),
                },
            )
            run["plan_approval"] = approval["id"]
            run["approvals"] = _dedupe([*run.get("approvals", []), approval["id"]])
            self.runs[run_id] = run
            insert_run(run)
        except Exception as exc:
            self._fail_run(run_id, exc)
        finally:
            self._executing.discard(run_id)

    def list_runs(self) -> list[dict[str, Any]]:
        mongo_runs = db_list_runs()
        if mongo_runs:
            return [self._serialize_run(run) for run in mongo_runs]
        return [self._serialize_run(run) for run in self.runs.values()]

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

    def delete_run(self, run_id: str) -> bool:
        """Remove a run and its approvals/evaluation. Returns False if unknown."""
        existed = self.get_run(run_id) is not None
        delete_run(run_id)
        self.runs.pop(run_id, None)
        self.evaluations.pop(run_id, None)
        for approval_id, approval in list(self.approvals.items()):
            if approval.get("run_id") == run_id:
                self.approvals.pop(approval_id, None)
        return existed

    def clear_runs(self) -> int:
        """Delete the entire run history and return the number of runs removed."""
        count = delete_all_runs()
        self.runs.clear()
        self.approvals.clear()
        self.evaluations.clear()
        return count

    def start_execution(self, run_id: str) -> None:
        """Kick the dev-team graph off in a background thread.

        A full run waits on provider rate limits and can take minutes, which is
        far too long to hold an HTTP request open. The request returns as soon as
        the run is marked ``running``; the console then polls for progress.
        """
        if run_id in self._executing:
            return
        run = self.get_run(run_id)
        if run is None:
            raise KeyError("Run not found")

        self._executing.add(run_id)
        run["status"] = "running"
        run["error"] = None
        run["updated_at"] = datetime.now(timezone.utc)
        self.runs[run_id] = run
        insert_run(run)

        thread = threading.Thread(
            target=self._execute_in_background,
            args=(run_id,),
            name=f"devteam-run-{run_id[:8]}",
            daemon=True,
        )
        thread.start()

    def _execute_in_background(self, run_id: str) -> None:
        try:
            run = self.get_run(run_id)
            if run is None:
                return
            run["current_stage"] = "creating_branch"
            run["status"] = "running"
            self.runs[run_id] = run
            insert_run(run)
            repository_slug = str(run.get("repository_slug") or "").strip()
            if not repository_slug:
                owner, repo = default_repo_parts()
                repository_slug = f"{owner}/{repo}" if owner and repo else ""
            branch_event_id = uuid4().hex
            branch_arguments = {"repository": repository_slug or "not selected"}
            self._record_system_tool_event(
                run_id,
                "github.prepare_branch",
                "running",
                branch_arguments,
                event_id=branch_event_id,
            )
            try:
                workspace = GitHubWorkspaceService().prepare_workspace(
                    run_id, repository_slug, str(run.get("message", ""))
                )
            except Exception as exc:
                self._record_system_tool_event(
                    run_id,
                    "github.prepare_branch",
                    "error",
                    branch_arguments,
                    error=str(exc),
                    event_id=branch_event_id,
                )
                raise
            run.update(workspace)
            run["repository_slug"] = workspace["repository_slug"]
            run["updated_at"] = datetime.now(timezone.utc)
            self.runs[run_id] = run
            insert_run(run)
            self._record_system_tool_event(
                run_id,
                "github.prepare_branch",
                "executed",
                {
                    "repository": workspace["repository_slug"],
                    "branch": workspace["branch_name"],
                    "base": workspace["base_branch"],
                },
                {"workspace_ready": True},
                event_id=branch_event_id,
            )
            with agent_run_context(workspace["workspace_root"], workspace["repository_slug"]):
                self.execute_run(run_id)
        except Exception as exc:  # a crash must still settle the run
            self._fail_run(run_id, exc)
        finally:
            self._executing.discard(run_id)

    def _record_system_tool_event(
        self,
        run_id: str,
        tool: str,
        status: str,
        arguments: dict[str, Any],
        result: dict[str, Any] | None = None,
        error: str | None = None,
        event_id: str | None = None,
    ) -> None:
        run = self.get_run(run_id)
        if run is None:
            return
        event = {
            "event_id": event_id or uuid4().hex,
            "agent": "backend",
            "tool": tool,
            "status": status,
            "arguments": arguments,
            "result": result,
            "error": error,
        }
        events = run.setdefault("tool_events", [])
        index = next(
            (i for i, item in enumerate(events) if item.get("event_id") == event["event_id"]),
            None,
        )
        if index is None:
            events.append(event)
        else:
            events[index] = event
        run["updated_at"] = datetime.now(timezone.utc)
        self.runs[run_id] = run
        insert_run(run)

    def _fail_run(self, run_id: str, exc: BaseException) -> None:
        run = self.get_run(run_id)
        if run is None:
            return
        run["status"] = "failed"
        run["error"] = f"{exc.__class__.__name__}: {str(exc)[:400]}"
        run["current_stage"] = None
        run["updated_at"] = datetime.now(timezone.utc)
        self.runs[run_id] = run
        insert_run(run)

    def _progress_sink(self, run_id: str) -> Any:
        """Persist each finished stage so ``GET /api/runs`` shows live progress."""

        def report(update: dict[str, Any]) -> None:
            run = self.get_run(run_id)
            if run is None:
                return
            run["stage_results"] = update.get("stage_results", [])
            backend_events = [
                event for event in run.get("tool_events", [])
                if event.get("agent") == "backend"
            ]
            run["tool_events"] = [*backend_events, *update.get("tool_events", [])]
            run["current_stage"] = update.get("stage")
            if run.get("status") not in ("completed", "rejected", "failed"):
                run["status"] = "running"
            run["updated_at"] = datetime.now(timezone.utc)
            self.runs[run_id] = run
            insert_run(run)

        return report

    def is_executing(self, run_id: str) -> bool:
        return run_id in self._executing

    def execute_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError("Run not found")
        execution_result = self.execution_service.execute_run(
            run, on_progress=self._progress_sink(run_id)
        )
        run["final_answer"] = execution_result["final_answer"]
        run["updated_at"] = execution_result["updated_at"]
        run["stage_results"] = execution_result["stage_results"]
        backend_events = [
            event for event in run.get("tool_events", [])
            if event.get("agent") == "backend"
        ]
        execution_result["tool_events"] = [
            *backend_events,
            *execution_result.get("tool_events", []),
        ]
        run["tool_events"] = execution_result["tool_events"]
        run["current_stage"] = None
        run["error"] = None

        deferred_actions = execution_result.get("deferred_actions", [])
        run["deferred_actions"] = deferred_actions
        run["action_results"] = []
        latest_stages: dict[str, dict[str, Any]] = {}
        for stage in reversed(execution_result.get("stage_results", [])):
            latest_stages.setdefault(str(stage.get("agent", "")), stage)
        required_stage_statuses = {
            "developer": "implementation_ready",
            "reviewer": "review_passed",
            "tester": "validated",
            "docs": "documented",
        }
        report_blocked = any(
            event.get("tool") in {"repo.write_file", "repo.run_command"}
            and event.get("status") in {"error", "blocked"}
            for event in execution_result.get("tool_events", [])
        ) or any(
            latest_stages.get(agent, {}).get("status") != expected
            for agent, expected in required_stage_statuses.items()
        )
        written_paths = []
        for event in execution_result.get("tool_events", []):
            if event.get("tool") != "repo.write_file" or event.get("status") not in {"executed", "ok"}:
                continue
            path = (event.get("arguments") or {}).get("file_path")
            if isinstance(path, str) and path not in written_paths:
                written_paths.append(path)

        if report_blocked:
            failed_stages = [
                f"{agent}: {latest_stages.get(agent, {}).get('summary') or 'stage did not pass'}"
                for agent, expected in required_stage_statuses.items()
                if latest_stages.get(agent, {}).get("status") != expected
            ]
            failed_tools = [
                f"{event.get('tool')}: {event.get('error') or 'tool call failed'}"
                for event in execution_result.get("tool_events", [])
                if event.get("tool") in {"repo.write_file", "repo.run_command"}
                and event.get("status") in {"error", "blocked"}
            ]
            details = "; ".join([*failed_stages, *failed_tools])[:700]
            raise RuntimeError(
                "Workflow stopped before final review because required checks did not pass. "
                f"{details} No pull request or Gmail report was created. Review the failed stage and retry."
            )

        documentation_pdf = write_run_documentation_pdf(
            str(run.get("workspace_root") or ""), run, written_paths
        )
        run["documentation_pdf_path"] = documentation_pdf
        written_paths.append(documentation_pdf)
        for action in deferred_actions:
            if action.get("purpose") == "final_run_report":
                arguments = action.setdefault("arguments", {})
                arguments["attachment_path"] = documentation_pdf

        workspace_review = GitHubWorkspaceService().prepare_review(
            run_id, str(run.get("workspace_root") or ""), written_paths
        )
        run["workspace_review"] = workspace_review

        # Successful runs publish their reviewed branch as a PR automatically so
        # a human can review it on GitHub. Any other external actions remain behind
        # their own approval gate.
        agent_actions = [
            action for action in deferred_actions
            if action.get("purpose") != "final_run_report"
            and action.get("tool") != "github.create_pull_request"
        ]
        pull_request_action = {
            "event_id": uuid4().hex,
            "agent": "backend",
            "tool": "github.create_pull_request",
            "purpose": "publish_run_pull_request",
            "arguments": {
                "run_id": run_id,
                "repository_slug": run.get("repository_slug"),
                "branch_name": run.get("branch_name"),
                "base_branch": run.get("base_branch"),
            },
        }
        report_actions = [
            action for action in deferred_actions
            if action.get("purpose") == "final_run_report"
        ]
        for action in report_actions:
            arguments = action.setdefault("arguments", {})
            body = str(arguments.get("body") or "")
            arguments["body"] = (
                body
                + f"\n\nPDF documentation: {documentation_pdf} (also attached)"
                + "\n\nPull request:\n{{PULL_REQUEST_URL}}"
            )[:15000]
        deferred_actions = [pull_request_action, *agent_actions, *report_actions]
        execution_result["deferred_actions"] = deferred_actions
        run["deferred_actions"] = deferred_actions
        action_event_ids = {action.get("event_id") for action in deferred_actions}
        implementation_events = [
            event for event in execution_result.get("tool_events", [])
            if event.get("event_id") not in action_event_ids
            and event.get("status") != "awaiting_approval"
        ]
        queued_action_events = [
            {
                "event_id": action.get("event_id"),
                "agent": action.get("agent"),
                "tool": action.get("tool"),
                "status": (
                    "running" if action is pull_request_action
                    else "queued" if action.get("purpose") == "final_run_report"
                    else "awaiting_approval"
                ),
                "arguments": action.get("arguments", {}),
            }
            for action in deferred_actions
        ]
        execution_result["tool_events"] = [*implementation_events, *queued_action_events]
        run["tool_events"] = execution_result["tool_events"]
        additional_actions = [
            action for action in deferred_actions
            if action is not pull_request_action
        ]
        run["status"] = "delivering"
        self.runs[run_id] = run
        insert_run(run)
        self._start_pr_publication(run_id, pull_request_action, additional_actions)
        return execution_result

    def _start_pr_publication(
        self,
        run_id: str,
        pull_request_action: dict[str, Any],
        additional_actions: list[dict[str, Any]],
    ) -> None:
        thread = threading.Thread(
            target=self._publish_pr_then_queue_actions,
            args=(run_id, pull_request_action, additional_actions),
            name=f"devteam-pr-{run_id[:8]}",
            daemon=True,
        )
        thread.start()

    def _publish_pr_then_queue_actions(
        self,
        run_id: str,
        pull_request_action: dict[str, Any],
        additional_actions: list[dict[str, Any]],
    ) -> None:
        self._execute_approved_actions(
            run_id,
            [pull_request_action],
            repository_work_failed=False,
            awaiting_pr_review=True,
        )
        run = self.get_run(run_id)
        if run is None:
            return

        pr_url = str(run.get("pull_request_url") or "").strip()
        if not pr_url:
            failure = run.get("error") or "GitHub did not return a pull request URL."
            blocked_actions = [
                {
                    "event_id": action.get("event_id"),
                    "agent": action.get("agent"),
                    "tool": action.get("tool"),
                    "status": "blocked",
                    "arguments": action.get("arguments", {}),
                    "error": f"This action was not run because the pull request was not created: {failure}",
                }
                for action in additional_actions
            ]
            run["action_results"] = [*run.get("action_results", []), *blocked_actions]
            run["deferred_actions"] = [pull_request_action, *additional_actions]
            run["error"] = _action_failure_message(
                [*run["action_results"]]
            )
            self._record_action_results(run, blocked_actions)
            return

        report_actions = [
            action for action in additional_actions
            if action.get("purpose") == "final_run_report"
        ]
        other_actions = [
            action for action in additional_actions
            if action.get("purpose") != "final_run_report"
        ]
        for action in report_actions:
            arguments = action.setdefault("arguments", {})
            arguments["body"] = str(arguments.get("body") or "").replace(
                "{{PULL_REQUEST_URL}}", pr_url
            )
        if report_actions:
            self._execute_approved_actions(
                run_id,
                report_actions,
                repository_work_failed=False,
                awaiting_pr_review=True,
            )
            run = self.get_run(run_id)
            if run is None:
                return

        if not other_actions:
            run["deferred_actions"] = [pull_request_action, *report_actions]
            self.runs[run_id] = run
            insert_run(run)
            return

        report_event_ids = {action.get("event_id") for action in report_actions}
        report_sent = any(
            result.get("event_id") in report_event_ids and result.get("status") == "executed"
            for result in run.get("action_results", [])
        )
        if report_sent:
            report_note = "The PDF report was emailed to the connected Gmail account. "
        elif report_actions:
            report_note = "The PDF report could not be sent; see workflow errors. "
        else:
            report_note = "No PDF report was sent because Gmail is not connected. "

        approval = self.create_approval(
            run_id,
            "execute_deferred_actions",
            {
                "stage": "post_pr_actions",
                "notes": (
                    f"The pull request is open for human review: {pr_url}. "
                    + report_note
                    + "Approve or reject these other external actions separately."
                ),
                "actions": other_actions,
            },
        )
        run["deferred_actions"] = [pull_request_action, *report_actions, *other_actions]
        run["action_approval"] = approval["id"]
        run["approvals"] = _dedupe([*run.get("approvals", []), approval["id"]])
        run["status"] = "waiting_for_pr_review"
        self.runs[run_id] = run
        insert_run(run)

    def _record_action_results(
        self, run: dict[str, Any], results: list[dict[str, Any]]
    ) -> None:
        events = run.setdefault("tool_events", [])
        for result in results:
            event_id = result.get("event_id")
            event = {
                "event_id": event_id,
                "agent": result.get("agent"),
                "tool": result.get("tool"),
                "status": result.get("status"),
                "error": result.get("error"),
                "arguments": result.get("arguments"),
                "result": result.get("result"),
            }
            index = next(
                (i for i, item in enumerate(events) if event_id and item.get("event_id") == event_id),
                None,
            )
            if index is None:
                events.append(event)
            else:
                events[index] = event
        run["status"] = "completed_with_errors"
        run["current_stage"] = None
        run["updated_at"] = datetime.now(timezone.utc)
        self.runs[str(run["id"])] = run
        insert_run(run)

    def create_approval(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        record = self.approval_service.create_approval(run_id, action, payload)
        self.approvals[record["id"]] = record
        insert_approval(record)
        return record

    def update_approval(self, approval_id: str, decision: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        # Prefer this process's copy, but fall back to the stored record: after a
        # restart the in-memory maps are empty while the gate is still pending.
        with self._approval_lock:
            approval = self.approvals.get(approval_id)
            if approval is None:
                approval = db_get_approval(approval_id)
            if approval is None:
                raise KeyError("Approval not found")
            if approval.get("status") != "pending":
                raise ValueError("This approval has already been decided.")

            approval = self.approval_service.update_approval(
                approval_id, decision, arguments, record=approval
            )
            self.approvals[approval_id] = approval
            db_update_approval(approval_id, approval)

        run = self.runs.get(approval["run_id"])
        if run is None:
            run = db_get_run(approval["run_id"])
        if run is None:
            return approval

        decided_at = approval.get("updated_at") or datetime.now(timezone.utc)
        run["updated_at"] = decided_at

        # Additional external actions are approved separately after the PR is open.
        if approval.get("action") in {"execute_deferred_actions", "approve_final_delivery"}:
            approval_payload = approval.get("payload") or {}
            deferred = approval_payload.get("actions")
            if not isinstance(deferred, list):
                deferred = run.get("deferred_actions", [])
            if decision == "approve":
                repository_work_failed = any(
                    event.get("tool") in {"repo.write_file", "repo.run_command"}
                    and event.get("status") in {"error", "blocked"}
                    for event in run.get("tool_events", [])
                )
                latest_stages: dict[str, dict[str, Any]] = {}
                for stage in reversed(run.get("stage_results", [])):
                    latest_stages.setdefault(str(stage.get("agent", "")), stage)
                required_stage_statuses = {
                    "developer": "implementation_ready",
                    "reviewer": "review_passed",
                    "tester": "validated",
                    "docs": "documented",
                }
                repository_work_failed = repository_work_failed or any(
                    latest_stages.get(agent, {}).get("status") != expected
                    for agent, expected in required_stage_statuses.items()
                )
                run["status"] = "delivering"
                run["updated_at"] = datetime.now(timezone.utc)
                self.runs[approval["run_id"]] = run
                insert_run(run)
                thread = threading.Thread(
                    target=self._execute_approved_actions,
                    args=(approval["run_id"], deferred, repository_work_failed),
                    name=f"devteam-delivery-{approval['run_id'][:8]}",
                    daemon=True,
                )
                thread.start()
                return approval
            else:
                skipped_actions = [
                    {
                        "event_id": item.get("event_id"),
                        "agent": item.get("agent"),
                        "tool": item.get("tool"),
                        "status": "skipped",
                        "arguments": item.get("arguments", {}),
                    }
                    for item in deferred
                ]
                skipped_ids = {item.get("event_id") for item in skipped_actions}
                run["action_results"] = [
                    item for item in run.get("action_results", [])
                    if item.get("event_id") not in skipped_ids
                ] + skipped_actions
            failed_actions = [
                result for result in run["action_results"]
                if result.get("status") in {"error", "blocked"}
            ]
            if decision == "reject" and run.get("pull_request_url"):
                run["status"] = "waiting_for_pr_review"
            elif decision == "reject":
                run["status"] = "rejected"
            elif failed_actions:
                run["status"] = "completed_with_errors"
            else:
                run["status"] = "completed"
            run["current_stage"] = None
            run["error"] = (
                _action_failure_message(failed_actions) if failed_actions else None
            )
            for result in run["action_results"]:
                event = {
                    "event_id": result.get("event_id"),
                    "agent": result.get("agent"),
                    "tool": result.get("tool"),
                    "status": result.get("status"),
                    "error": result.get("error"),
                    "arguments": result.get("arguments"),
                    "result": result.get("result"),
                }
                event_id = event.get("event_id")
                index = next(
                    (
                        i for i, item in enumerate(run.get("tool_events", []))
                        if event_id and item.get("event_id") == event_id
                    ),
                    None,
                )
                if index is None:
                    run.setdefault("tool_events", []).append(event)
                else:
                    run["tool_events"][index] = event
            self.runs[approval["run_id"]] = run
            insert_run(run)
            return approval

        # Gate 1: approving the plan is the only way to create a branch and start agents.
        if approval.get("action") == "approve_plan_before_execution" and decision == "approve":
            payload = approval.get("payload") or {}
            repository_slug = str(payload.get("repository_slug") or run.get("repository_slug") or "")
            if not repository_slug:
                owner, repo = default_repo_parts()
                repository_slug = f"{owner}/{repo}" if owner and repo else ""
            run["repository_slug"] = repository_slug or None
            run["status"] = "approved"
            run["approvals"] = _dedupe([*run.get("approvals", []), approval_id])
            self.runs[approval["run_id"]] = run
            insert_run(run)
            # Never block the request on the whole workflow: it runs in the
            # background and the console follows it via polling.
            self.start_execution(approval["run_id"])
        elif approval.get("action") == "approve_plan_before_execution":
            run["status"] = "rejected"
            run["approvals"] = _dedupe([*run.get("approvals", []), approval_id])
            self.runs[approval["run_id"]] = run
            insert_run(run)

        return approval

    def _execute_approved_actions(
        self,
        run_id: str,
        deferred: list[dict[str, Any]],
        repository_work_failed: bool,
        awaiting_pr_review: bool = False,
    ) -> None:
        run = self.get_run(run_id)
        if run is None:
            return

        def report_action_event(event: dict[str, Any]) -> None:
            current = self.get_run(run_id)
            if current is None:
                return
            event_id = event.get("event_id")
            events = current.setdefault("tool_events", [])
            index = next(
                (i for i, item in enumerate(events) if event_id and item.get("event_id") == event_id),
                None,
            )
            if index is None:
                events.append(event)
            else:
                events[index] = event
            current["updated_at"] = datetime.now(timezone.utc)
            self.runs[run_id] = current
            insert_run(current)

        workspace_root = str(run.get("workspace_root") or "")
        repository_slug = str(run.get("repository_slug") or "")
        if workspace_root and repository_slug:
            with agent_run_context(workspace_root, repository_slug):
                action_results = self.execution_service.execute_deferred(
                    deferred,
                    run_context=run,
                    block_report=repository_work_failed,
                    on_event=report_action_event,
                )
        else:
            action_results = self.execution_service.execute_deferred(
                deferred,
                run_context=run,
                block_report=repository_work_failed,
                on_event=report_action_event,
            )
        run = self.get_run(run_id)
        if run is None:
            return
        completed_event_ids = {result.get("event_id") for result in action_results}
        run["action_results"] = [
            result for result in run.get("action_results", [])
            if result.get("event_id") not in completed_event_ids
        ] + action_results
        failed_actions = [
            result for result in run["action_results"]
            if result.get("status") in {"error", "blocked"}
        ]
        successful_pr = next(
            (
                item for item in action_results
                if item.get("tool") == "github.create_pull_request"
                and item.get("status") == "executed"
                and isinstance(item.get("result"), dict)
            ),
            None,
        )
        if successful_pr:
            pr_result = successful_pr["result"]
            pr_url = str(pr_result.get("url") or "").strip()
            if pr_url:
                run["pull_request_url"] = pr_url
                run["final_answer"] = (
                    str(run.get("final_answer") or "").rstrip()
                    + f"\n\nPull request: {pr_url}"
                )
        if failed_actions:
            run["status"] = "completed_with_errors"
        elif awaiting_pr_review and run.get("pull_request_url"):
            run["status"] = "waiting_for_pr_review"
        else:
            run["status"] = "completed"
        run["current_stage"] = None
        run["error"] = (
            _action_failure_message(failed_actions) if failed_actions else None
        )
        run["updated_at"] = datetime.now(timezone.utc)
        self.runs[run_id] = run
        insert_run(run)

    def _serialize_run(self, run: dict[str, Any]) -> dict[str, Any]:
        run_error = run.get("error")
        if (
            run.get("status") == "completed_with_errors"
            and isinstance(run_error, str)
            and run_error.startswith("Delivery finished with issues:")
        ):
            action_results = run.get("action_results", [])
            failures = [
                result for result in action_results
                if isinstance(result, dict) and result.get("status") in {"error", "blocked"}
            ] if isinstance(action_results, list) else []
            if failures:
                run_error = _action_failure_message(failures)
        return {
            "id": run.get("id"),
            "message": run.get("message"),
            "status": run.get("status"),
            "current_stage": run.get("current_stage"),
            "created_at": run.get("created_at"),
            "updated_at": run.get("updated_at"),
            "tasks": run.get("tasks", []),
            "approvals": run.get("approvals", []),
            "final_answer": run.get("final_answer"),
            "error": run_error,
            "tool_access": run.get("tool_access", {}),
            "stage_results": run.get("stage_results", []),
            "tool_events": run.get("tool_events", []),
            "deferred_actions": run.get("deferred_actions", []),
            "action_results": run.get("action_results", []),
            "action_approval": run.get("action_approval"),
            "plan_approval": run.get("plan_approval"),
            "plan_summary": run.get("plan_summary"),
            "repository_slug": run.get("repository_slug"),
            "branch_name": run.get("branch_name"),
            "base_branch": run.get("base_branch"),
            "workspace_review": run.get("workspace_review"),
            "pull_request_url": run.get("pull_request_url"),
        }

    def serialize_run(self, run: dict[str, Any]) -> dict[str, Any]:
        """Return the public run view, excluding backend-only workspace paths."""
        return self._serialize_run(run)

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

