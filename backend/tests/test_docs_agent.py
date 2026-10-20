from __future__ import annotations

from app.agents.docs import DocumentationAgent
from app.services.agent_run_context import agent_run_context


def test_docs_writes_report_from_run_state_without_model_calls(tmp_path, monkeypatch) -> None:
    agent = DocumentationAgent()
    original_work = agent.work
    allowed_tools: list[str] = []

    def record_tool_allowlist(**kwargs):
        allowed_tools.extend(kwargs["tool_allowlist"])
        return original_work(**kwargs)

    monkeypatch.setattr(agent, "work", record_tool_allowlist)
    state = {
        "run_id": "0545b7a7-workflow",
        "request": "Add GET /api/ping returning {\"message\": \"pong\"}.",
        "stage_results": [
            {"agent": "developer", "status": "implementation_ready", "summary": "Added ping."},
            {"agent": "reviewer", "status": "review_passed", "summary": "Review passed."},
            {"agent": "tester", "status": "validated", "summary": "Tests passed."},
        ],
        "tool_events": [
            {
                "tool": "repo.write_file",
                "status": "executed",
                "arguments": {"file_path": "backend/app/api/routes.py"},
            },
        ],
    }

    with agent_run_context(tmp_path, "owner/repository"):
        result = agent.run(state)

    report_path = tmp_path / "docs" / "ai-dev-team-run-0545b7a7.md"
    report = report_path.read_text(encoding="utf-8")
    assert result["stage_results"][0]["status"] == "documented"
    assert "repo.read_file" not in allowed_tools
    assert "Add GET /api/ping" in report
    assert "backend/app/api/routes.py" in report
    assert "tester: validated - Tests passed." in report


def test_docs_recovers_report_write_after_tool_limit(tmp_path, monkeypatch) -> None:
    agent = DocumentationAgent()

    def blocked_model_write(**kwargs):
        report_path = kwargs["fallback_calls"][0][1]["file_path"]
        return (
            {
                "agent": "docs",
                "status": "documented",
                "summary": "The model attempted the report write.",
                "details": {"engine": "llm", "deferred_actions": []},
            },
            [{
                "tool": "repo.write_file",
                "status": "blocked",
                "arguments": {"file_path": report_path},
                "error": "The docs tool-call limit (4) was reached.",
            }],
        )

    monkeypatch.setattr(agent, "work", blocked_model_write)

    with agent_run_context(tmp_path, "owner/repository"):
        result = agent.run({"run_id": "run-12345678", "request": "Write a report."})

    assert result["stage_results"][0]["status"] == "documented"
    blocked_event = next(
        event for event in result["tool_events"]
        if event.get("error") == "The docs tool-call limit (4) was reached."
    )
    assert blocked_event["status"] == "recovered"
