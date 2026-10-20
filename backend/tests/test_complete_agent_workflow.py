from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from app.mcp.repository import RepositoryMCPClient
from app.services.agent_run_context import agent_run_context
from app.services.llm_service import llm_service
from app.workflow.graph import build_workflow


class ScriptedModel:
    """Replay model replies so the entire agent graph runs offline."""

    def __init__(self, replies: list[AIMessage]) -> None:
        self.replies = list(replies)

    def invoke(self, messages: list[Any]) -> AIMessage:
        if self.replies:
            return self.replies.pop(0)
        return AIMessage(content="Done.")


def _call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": arguments, "id": call_id, "type": "tool_call"}


def _tool_reply(*calls: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls))


def test_complete_agent_workflow_runs_all_stages_and_defers_external_actions(
    tmp_path, monkeypatch
) -> None:
    models = iter(
        [
            ScriptedModel(
                [
                    _tool_reply(_call("repo__list_files", {"relative_path": "."}, "plan-read")),
                    AIMessage(content="Plan: implement, review, validate, document."),
                ]
            ),
            ScriptedModel(
                [
                    _tool_reply(
                        _call(
                            "repo__write_file",
                            {"file_path": "src/feature.py", "content": "ready = False\n"},
                            "write-first",
                        )
                    ),
                    AIMessage(content="Implemented the feature."),
                ]
            ),
            ScriptedModel(
                [
                    _tool_reply(
                        _call("repo__read_file", {"file_path": "src/feature.py"}, "review-first")
                    ),
                    AIMessage(content="Please correct the default. VERDICT: CHANGES_REQUESTED"),
                ]
            ),
            ScriptedModel(
                [
                    _tool_reply(
                        _call(
                            "repo__write_file",
                            {"file_path": "src/feature.py", "content": "ready = True\n"},
                            "write-second",
                        )
                    ),
                    AIMessage(content="Corrected the default."),
                ]
            ),
            ScriptedModel(
                [
                    _tool_reply(
                        _call("repo__read_file", {"file_path": "src/feature.py"}, "review-second")
                    ),
                    AIMessage(content="The implementation is correct. VERDICT: APPROVED"),
                ]
            ),
            ScriptedModel(
                [
                    _tool_reply(
                        _call(
                            "repo__run_command",
                            {"command": "python -m pytest backend/tests -q"},
                            "validate",
                        )
                    ),
                    AIMessage(content="Validation passed."),
                ]
            ),
            ScriptedModel(
                [
                    _tool_reply(
                        _call(
                            "repo__write_file",
                            {"file_path": "docs/feature.md", "content": "Feature documented.\n"},
                            "document",
                        ),
                        _call(
                            "gmail__send_email",
                            {"to": "owner@example.com", "subject": "Run complete", "body": "Report"},
                            "email",
                        ),
                    ),
                    AIMessage(content="Documented the change."),
                ]
            ),
        ]
    )
    reusable_model: ScriptedModel | None = None
    reuse_after_required_call = False

    def get_model(**options):
        nonlocal reusable_model, reuse_after_required_call
        if options.get("tool_choice") == "required":
            reusable_model = next(models)
            reuse_after_required_call = True
            return reusable_model
        if reuse_after_required_call:
            reuse_after_required_call = False
            return reusable_model
        return next(models)

    monkeypatch.setattr(llm_service, "get_chat_model", get_model)
    monkeypatch.setattr(llm_service, "summarize_run", lambda request, stages: "All stages passed.")
    monkeypatch.setattr(
        RepositoryMCPClient,
        "run_command",
        lambda self, command, timeout=120: {
            "command": command,
            "return_code": 0,
            "stdout": "2 passed",
            "stderr": "",
            "timed_out": False,
        },
    )

    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "tests").mkdir()
    state = {
        "run_id": "offline-complete-agent-test",
        "request": "Add a feature and document it.",
        "plan": [{"id": "implement", "title": "Implement the feature", "owner_agent": "developer"}],
        "stage_results": [],
        "tool_events": [],
        "current_stage": "planner",
        "review_attempts": 0,
        "review_passed": False,
        "status": "running",
        "final_answer": None,
        "error": None,
    }

    with agent_run_context(tmp_path, "acme/widget"):
        result = build_workflow().invoke(state)

    assert result["status"] == "completed"
    assert result["final_answer"] == "All stages passed."
    assert [stage["agent"] for stage in result["stage_results"]] == [
        "planner",
        "developer",
        "reviewer",
        "developer",
        "reviewer",
        "tester",
        "docs",
    ]
    assert [stage["status"] for stage in result["stage_results"]] == [
        "planned",
        "implementation_ready",
        "changes_requested",
        "implementation_ready",
        "review_passed",
        "validated",
        "documented",
    ]
    assert (tmp_path / "src" / "feature.py").read_text(encoding="utf-8") == "ready = True\n"
    assert (tmp_path / "docs" / "feature.md").is_file()

    email_events = [event for event in result["tool_events"] if event["tool"] == "gmail.send_email"]
    assert len(email_events) == 1
    assert email_events[0]["status"] == "awaiting_approval"


def test_failed_developer_stops_before_review_and_validation(monkeypatch) -> None:
    models = iter(
        [
            ScriptedModel([AIMessage(content="Plan ready.")]),
            ScriptedModel([AIMessage(content="I could not make a change.")]),
            ScriptedModel([AIMessage(content="I still could not apply a change.")]),
        ]
    )
    monkeypatch.setattr(llm_service, "get_chat_model", lambda **_: next(models))

    result = build_workflow().invoke(
        {
            "run_id": "offline-implementation-failure-test",
            "request": "Add a feature.",
            "plan": [{"id": "implement", "title": "Implement the feature", "owner_agent": "developer"}],
            "stage_results": [],
            "tool_events": [],
            "current_stage": "planner",
            "review_attempts": 0,
            "review_passed": False,
            "status": "running",
            "final_answer": None,
            "error": None,
        }
    )

    assert result["status"] == "failed"
    assert [stage["agent"] for stage in result["stage_results"]] == ["planner", "developer"]
    developer_stage = result["stage_results"][-1]
    assert developer_stage["details"]["recovery_attempted"] is True
    assert "after one recovery attempt" in developer_stage["summary"]
    assert "no reviewable change" in result["final_answer"]
