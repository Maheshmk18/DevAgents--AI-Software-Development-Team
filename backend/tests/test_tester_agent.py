from __future__ import annotations

from typing import Any

from app.agents.tester import TestingAgent as AgentUnderTest


def test_collection_error_in_changed_test_is_repairable(monkeypatch) -> None:
    agent = AgentUnderTest()

    def failed_collection(**_: Any):
        return (
            {
                "agent": "tester",
                "status": "validated",
                "summary": "Validation command completed.",
                "details": {},
            },
            [
                {
                    "tool": "repo.run_command",
                    "status": "error",
                    "error": "Pytest could not collect tests.",
                    "result": {
                        "collection_failed": True,
                        "collection_error_paths": [
                            r"C:\workspace\backend\tests\test_hello.py"
                        ],
                    },
                }
            ],
        )

    monkeypatch.setattr(agent, "work", failed_collection)
    state = {
        "request": "Update the hello endpoint and its test.",
        "stage_results": [{"agent": "reviewer", "summary": "Approved."}],
        "tool_events": [
            {
                "tool": "repo.write_file",
                "status": "executed",
                "arguments": {"file_path": "backend/tests/test_hello.py"},
            }
        ],
    }

    result = agent.run(state)

    entry = result["stage_results"][0]
    assert entry["status"] == "validation_failed"
    assert entry["details"]["repairable"] is True
    assert entry["details"]["matching_collection_change"] is True
    assert "changed test file" in entry["summary"]
