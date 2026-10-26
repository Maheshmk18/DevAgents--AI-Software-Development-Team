from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from app.core.util import truncate
from app.agents.runtime import reset_tool_event_sink, set_tool_event_sink
from app.integrations.gmail import GmailClient
from app.mcp.tool_registry import MCPToolRegistry
from app.services.llm_service import llm_service
from app.services.connector_service import gmail_report_recipient
from app.services.github_workspace import GitHubWorkspaceService
from app.workflow.graph import get_workflow
from app.workflow.orchestrator import STAGE_ORDER

#: Called after every graph node with the partial state, so the console can show
#: a run progressing instead of a frozen spinner.
ProgressCallback = Callable[[dict[str, Any]], None]


class ExecutionService:
    """Runs the dev-team LangGraph workflow and normalises its result."""

    def __init__(self) -> None:
        self.stage_order = list(STAGE_ORDER)
        self.registry = MCPToolRegistry()

    def execute_run(
        self,
        run: dict[str, Any],
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        run_id = str(run.get("id", ""))
        request = str(run.get("message", ""))

        initial_state: dict[str, Any] = {
            "run_id": run_id,
            "request": request,
            "plan": run.get("tasks") or [],
            "stage_results": [],
            "tool_events": [],
            "review_attempts": 0,
            "review_passed": True,
        }

        # ``stream`` yields the partial update of each node as it finishes, which
        # is what lets the UI watch the team work in real time. ``stage_results``
        # and ``tool_events`` are append-only in the graph, so the deltas are
        # re-accumulated here into the same shape ``invoke`` would have returned.
        stage_results: list[dict[str, Any]] = []
        tool_events: list[dict[str, Any]] = []
        final_state: dict[str, Any] = {}

        def on_tool_event(event: dict[str, Any]) -> None:
            event_id = event.get("event_id")
            existing_index = next(
                (i for i, item in enumerate(tool_events) if event_id and item.get("event_id") == event_id),
                None,
            )
            if existing_index is None:
                tool_events.append(event)
            else:
                tool_events[existing_index] = event
            if on_progress is not None:
                self._report(
                    on_progress,
                    {
                        "node": event.get("agent"),
                        "stage": event.get("agent") or "running",
                        "status": "running",
                        "stage_results": list(stage_results),
                        "tool_events": list(tool_events),
                    },
                )

        sink_token = set_tool_event_sink(on_tool_event)
        try:
            for chunk in get_workflow().stream(initial_state):
                for node, payload in (chunk or {}).items():
                    if not isinstance(payload, dict):
                        continue
                    stage_results.extend(payload.get("stage_results") or [])
                    final_state.update(payload)
                    if on_progress is not None:
                        self._report(
                            on_progress,
                            {
                                "node": node,
                                "stage": payload.get("current_stage") or node,
                                "status": payload.get("status") or "running",
                                "stage_results": list(stage_results),
                                "tool_events": list(tool_events),
                            },
                        )
        finally:
            reset_tool_event_sink(sink_token)

        deferred_actions = self.collect_deferred_actions(stage_results)
        final_answer = final_state.get("final_answer") or llm_service.summarize_run(
            request, stage_results
        )
        report_recipient = gmail_report_recipient()
        if report_recipient:
            # The final report is system-generated and always goes to the
            # connected mailbox. Ignore any model-authored email proposals so
            # an agent cannot redirect this notification to another recipient.
            deferred_actions = [
                action for action in deferred_actions if action.get("tool") != "gmail.send_email"
            ]
            deferred_actions.append(
                self._final_report_action(request, stage_results, final_answer, report_recipient)
            )
        else:
            final_answer = (
                str(final_answer).rstrip()
                + "\n\nGmail report not queued: connect Gmail in the console Integrations settings, "
                "then start a new run to receive the PDF report. Google sign-in alone does not "
                "grant Gmail sending access."
            )
        recorded_event_ids = {event.get("event_id") for event in tool_events}
        for action in deferred_actions:
            event_id = action.get("event_id")
            if event_id and event_id not in recorded_event_ids:
                tool_events.append(
                    {
                        "event_id": event_id,
                        "agent": action.get("agent"),
                        "tool": action.get("tool"),
                        "status": "awaiting_approval",
                        "arguments": action.get("arguments", {}),
                    }
                )
                recorded_event_ids.add(event_id)

        return {
            "run_id": run_id,
            "status": final_state.get("status", "completed"),
            "stage_results": stage_results,
            "tool_events": tool_events,
            "deferred_actions": deferred_actions,
            "final_answer": final_answer,
            "updated_at": datetime.now(timezone.utc),
        }

    @staticmethod
    def _final_report_action(
        request: str,
        stage_results: list[dict[str, Any]],
        final_answer: str,
        recipient: str,
    ) -> dict[str, Any]:
        title = " ".join(request.strip().split())[:72] or "Completed workflow"
        stage_lines = [
            f"- {item.get('agent', 'agent')}: {item.get('summary', '')}"
            for item in stage_results
        ]
        body = (
            "AI Dev Team workflow report\n\n"
            f"Request:\n{request.strip()}\n\n"
            f"Final summary:\n{final_answer}\n\n"
            "Agent stages:\n"
            + "\n".join(stage_lines)
            + "\n\nPull request:\n{{PULL_REQUEST_URL}}"
        )[:15000]
        return {
            "event_id": uuid4().hex,
            "agent": "docs",
            "tool": "gmail.send_email",
            "arguments": {
                "to": recipient,
                "subject": f"AI Dev Team report: {title}",
                "body": body,
            },
            "purpose": "final_run_report",
        }

    @staticmethod
    def _report(on_progress: ProgressCallback, update: dict[str, Any]) -> None:
        """Never let a progress-reporting bug abort a run that is going well."""
        try:
            on_progress(update)
        except Exception:
            return


    @staticmethod
    def collect_deferred_actions(stage_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Gather unique side-effecting proposals from every stage."""
        seen: set[tuple[str, str]] = set()
        actions: list[dict[str, Any]] = []
        for entry in stage_results:
            for action in entry.get("details", {}).get("deferred_actions", []) or []:
                if not isinstance(action, dict) or not action.get("tool"):
                    continue
                key = (
                    action["tool"],
                    json.dumps(action.get("arguments", {}), sort_keys=True, default=str),
                )
                if key in seen:
                    continue
                seen.add(key)
                actions.append(action)
        return actions

    def execute_deferred(
        self,
        actions: list[dict[str, Any]],
        *,
        run_context: dict[str, Any] | None = None,
        block_report: bool = False,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute approved side-effecting actions and record the outcome."""
        results: list[dict[str, Any]] = []
        for action in actions:
            tool_name = str(action.get("tool", ""))
            arguments = action.get("arguments") or {}
            event_id = str(action.get("event_id") or uuid4().hex)
            if action.get("purpose") == "final_run_report" and (
                block_report or any(result.get("status") in {"error", "blocked"} for result in results)
            ):
                blocked = {
                    "event_id": event_id,
                    "agent": action.get("agent"),
                    "tool": tool_name,
                    "status": "blocked",
                    "arguments": arguments,
                    "error": "The report was not sent because implementation, validation, or documentation did not complete successfully.",
                }
                results.append(blocked)
                self._emit_action_event(on_event, blocked)
                continue
            self._emit_action_event(
                on_event,
                {
                    "event_id": event_id,
                    "agent": action.get("agent"),
                    "tool": tool_name,
                    "status": "running",
                    "arguments": arguments,
                },
            )
            try:
                if action.get("purpose") == "final_run_report":
                    current_recipient = gmail_report_recipient()
                    queued_recipient = str(arguments.get("to") or "").strip()
                    if not current_recipient or queued_recipient.casefold() != current_recipient.casefold():
                        raise RuntimeError(
                            "The connected Gmail account changed after this report was queued. "
                            "Start a new run to send the report to the current account."
                        )
                    pull_request = next(
                        (
                            item for item in results
                            if item.get("tool") == "github.create_pull_request"
                            and item.get("status") == "executed"
                        ),
                        None,
                    )
                    pr_result = (pull_request or {}).get("result")
                    pr_url = str(pr_result.get("url") or "").strip() if isinstance(pr_result, dict) else ""
                    arguments = dict(arguments)
                    body = str(arguments.get("body") or "")
                    arguments["body"] = body.replace(
                        "{{PULL_REQUEST_URL}}",
                        pr_url or "The pull request could not be created; see the workflow report for details.",
                    )
                if action.get("purpose") == "publish_run_pull_request":
                    pull_request_run = dict(run_context or {})
                    pull_request_run.update(arguments)
                    result = GitHubWorkspaceService().publish_pull_request(pull_request_run)
                    meta = {"result": result, "via": "github-api"}
                elif action.get("purpose") == "final_run_report" and arguments.get("attachment_path"):
                    report_context = run_context or {}
                    run_id = str(report_context.get("id") or "")
                    workspace_root_value = str(report_context.get("workspace_root") or "")
                    if not run_id or not workspace_root_value:
                        raise RuntimeError("The report PDF has no approved isolated workflow workspace.")
                    workspace_root = GitHubWorkspaceService()._validate_workspace_path(
                        run_id, workspace_root_value
                    )
                    attachment = (workspace_root / str(arguments["attachment_path"])).resolve()
                    try:
                        attachment.relative_to(workspace_root)
                    except ValueError as exc:
                        raise RuntimeError("The report PDF path is outside the isolated workflow workspace.") from exc
                    if attachment.suffix.lower() != ".pdf" or not attachment.is_file():
                        raise RuntimeError("The approved report PDF is missing from the isolated workflow branch.")
                    if attachment.stat().st_size > 10 * 1024 * 1024:
                        raise RuntimeError("The report PDF is larger than the 10 MB email attachment limit.")
                    result = GmailClient().send_email(
                        to=str(arguments.get("to") or ""),
                        subject=str(arguments.get("subject") or "AI Dev Team workflow report"),
                        body=str(arguments.get("body") or ""),
                        attachments=[(attachment.name, attachment.read_bytes(), "application/pdf")],
                    )
                    meta = {"result": result, "via": "gmail-api"}
                else:
                    meta = self.registry.call_tool_with_meta(tool_name, arguments)
                    result = meta.get("result")
                command_failed = (
                    tool_name == "repo.run_command"
                    and isinstance(result, dict)
                    and result.get("return_code") != 0
                )
                outcome = {
                    "event_id": event_id,
                    "agent": action.get("agent"),
                    "tool": tool_name,
                    "status": "error" if command_failed else "executed",
                    "arguments": arguments,
                    "via": meta.get("via"),
                    "result": truncate(result),
                    "error": (
                        str(result.get("stderr") or result.get("stdout") or "Command failed.")[:1200]
                        if command_failed
                        else None
                    ),
                }
                results.append(outcome)
                self._emit_action_event(on_event, outcome)
            except Exception as exc:
                outcome = {
                    "event_id": event_id,
                    "agent": action.get("agent"),
                    "tool": tool_name,
                    "status": "error",
                    "arguments": arguments,
                    "error": str(exc),
                }
                results.append(outcome)
                self._emit_action_event(on_event, outcome)
        return results

    @staticmethod
    def _emit_action_event(
        on_event: Callable[[dict[str, Any]], None] | None,
        event: dict[str, Any],
    ) -> None:
        if on_event is None:
            return
        try:
            on_event(dict(event))
        except Exception:
            return
