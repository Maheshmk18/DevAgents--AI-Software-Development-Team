from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent
from app.agents.runtime import publish_tool_event
from app.workflow.state import DevTeamState

SYSTEM_PROMPT = (
    "You are the DOCS agent on a five-agent software engineering team "
    "(planner, developer, reviewer, tester, docs).\n"
    "Your job is to capture documentation deliverables from the completed work.\n\n"
    "How to work:\n"
    "- Use the request, changed-file list, and prior-stage results supplied in the prompt; "
    "do not read files or rerun tests.\n"
    "- Write any requested project documentation, then write the concise workflow report "
    "at the required report path.\n"
    "- If a follow-up should be filed or emailed, propose it with github.create_issue or "
    "gmail.send_email; these actions wait for human approval.\n"
    "- Finish with a short summary of the documentation deliverables."
)


class DocumentationAgent(BaseAgent):
    """Captures documentation deliverables and writes the workflow report."""

    name = "docs"

    def run(self, state: DevTeamState) -> dict[str, Any]:
        request = state.get("request", "")
        changed_files = list(dict.fromkeys(
            str(event.get("arguments", {}).get("file_path"))
            for event in state.get("tool_events", [])
            if event.get("tool") == "repo.write_file"
            and event.get("status") == "executed"
            and event.get("arguments", {}).get("file_path")
        ))
        prior_stages = [
            f"{item.get('agent')}: {item.get('status')} - {item.get('summary')}"
            for item in state.get("stage_results", [])
            if item.get("agent") in {"developer", "reviewer", "tester"}
        ]
        run_id = "".join(
            character for character in str(state.get("run_id") or "") if character.isalnum()
        )[:8] or "latest"
        report_path = f"docs/ai-dev-team-run-{run_id}.md"
        report_content = "\n".join(
            [
                "# Workflow Report",
                "",
                f"## Request\n\n{request.strip()}",
                "",
                "## Changed files",
                *(f"- {path}" for path in changed_files),
                "",
                "## Agent results",
                *(f"- {stage}" for stage in prior_stages),
                "",
            ]
        )
        fallback_calls = [(
            "repo.write_file",
            {
                "file_path": report_path,
                "content": report_content,
                "allow_shrink": True,
            },
        )]

        entry, events = self.work(
            status="documented",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=(
                f"Feature request:\n{request}\n\n"
                f"Applied files: {', '.join(changed_files) or '(none)'}\n\n"
                f"Implementation/review/validation results:\n"
                f"{chr(10).join(prior_stages) or '(none)'}\n\n"
                f"Write a concise Markdown workflow report to {report_path}. "
                "Use only the supplied run evidence; do not inspect repository files."
            ),
            fallback_calls=fallback_calls,
            fallback_summary="Generated a workflow report from the recorded run results.",
            force_initial_tool_call=True,
            tool_allowlist=["repo.write_file", "gmail.send_email", "github.create_issue"],
        )

        written = self._has_report_write(events, report_path)
        fallback_write_used = False
        if not written:
            fallback_events, _ = self.invoke_tools(fallback_calls)
            events.extend(fallback_events)
            written = self._has_report_write(events, report_path)
            fallback_write_used = written

        # The model may add feature-specific documentation, but the canonical
        # workflow report always comes from structured run evidence.
        if entry.get("details", {}).get("engine") == "llm" and not fallback_write_used:
            fallback_events, _ = self.invoke_tools(fallback_calls)
            events.extend(fallback_events)
            written = self._has_report_write(events, report_path)
            fallback_write_used = written

        if written:
            self._mark_recovered_report_writes(events, report_path)
            entry["status"] = "documented"
            entry["summary"] = "Generated a complete workflow report from the recorded run results."
        elif entry.get("details", {}).get("engine") == "deterministic":
            entry["status"] = "documentation_missing"
            entry["summary"] = "The docs agent could not write the workflow report. Review the tool activity."
        else:
            entry["status"] = "documentation_missing"
            entry["summary"] = (
                "The docs agent could not write the workflow report after its deterministic fallback. "
                "Review the tool activity and retry."
            )
        entry.setdefault("details", {})["report_path"] = report_path
        entry["details"]["fallback_write_used"] = fallback_write_used
        return {"current_stage": self.name, "tool_events": events, "stage_results": [entry]}

    @staticmethod
    def _has_report_write(events: list[dict[str, Any]], report_path: str) -> bool:
        return any(
            event.get("tool") == "repo.write_file"
            and event.get("status") == "executed"
            and event.get("arguments", {}).get("file_path") == report_path
            for event in events
        )

    @staticmethod
    def _mark_recovered_report_writes(
        events: list[dict[str, Any]],
        report_path: str,
    ) -> None:
        if not DocumentationAgent._has_report_write(events, report_path):
            return
        for event in events:
            if (
                event.get("tool") == "repo.write_file"
                and event.get("status") in {"error", "blocked"}
                and event.get("arguments", {}).get("file_path") == report_path
            ):
                event["status"] = "recovered"
                event["resolution"] = "A complete deterministic workflow report was written afterward."
                publish_tool_event(event)
