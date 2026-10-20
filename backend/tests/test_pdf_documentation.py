from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser

from app.integrations import gmail as gmail_module
from app.integrations.gmail import GmailClient
from app.services import execution_service as execution_module
from app.services.execution_service import ExecutionService
from app.services.github_workspace import GitHubWorkspaceService
from app.services.pdf_documentation import write_run_documentation_pdf


def test_run_documentation_is_written_as_a_pdf(tmp_path) -> None:
    documentation = tmp_path / "docs" / "overview.md"
    documentation.parent.mkdir()
    documentation.write_text("# API\nThe endpoint returns a health status.", encoding="utf-8")
    run = {
        "id": "a" * 32,
        "message": "Add a health endpoint",
        "final_answer": "Implemented and validated.",
        "repository_slug": "acme/widget",
        "branch_name": "agent/add-health-endpoint-aaaaaaaa",
        "stage_results": [{"agent": "tester", "status": "validated", "summary": "Tests passed."}],
        "tool_events": [{
            "tool": "repo.run_command",
            "status": "executed",
            "arguments": {"command": "pytest -q"},
            "result": {"return_code": 0},
        }],
    }

    relative_path = write_run_documentation_pdf(str(tmp_path), run, ["docs/overview.md"])

    pdf = (tmp_path / relative_path).read_bytes()
    assert relative_path.endswith(".pdf")
    assert pdf.startswith(b"%PDF-1.4")
    assert b"pytest -q" in pdf
    assert pdf.endswith(b"%%EOF\n")


def test_final_report_attaches_only_a_pdf_from_the_isolated_workspace(monkeypatch, tmp_path) -> None:
    run_id = "b" * 32
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    report_path = workspace / "docs" / "report.pdf"
    report_path.parent.mkdir()
    report_path.write_bytes(b"%PDF-1.4\nreport\n%%EOF")
    captured: dict = {}

    monkeypatch.setattr(execution_module, "gmail_report_recipient", lambda: "owner@example.com")
    monkeypatch.setattr(
        GitHubWorkspaceService,
        "_validate_workspace_path",
        lambda self, actual_run_id, actual_root: workspace,
    )

    def send_email(self, **kwargs):
        captured.update(kwargs)
        return {"status": "sent", "message_id": "mail-1"}

    monkeypatch.setattr(GmailClient, "send_email", send_email)
    service = ExecutionService()
    actions = [{
        "event_id": "mail-1",
        "agent": "docs",
        "tool": "gmail.send_email",
        "purpose": "final_run_report",
        "arguments": {
            "to": "owner@example.com",
            "subject": "Run report",
            "body": "Report body",
            "attachment_path": "docs/report.pdf",
        },
    }]

    result = service.execute_deferred(
        actions,
        run_context={"id": run_id, "workspace_root": str(workspace)},
    )

    assert result[0]["status"] == "executed"
    assert captured["attachments"] == [("report.pdf", b"%PDF-1.4\nreport\n%%EOF", "application/pdf")]


def test_gmail_encodes_pdf_as_a_mime_attachment(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        gmail_module.credential_store,
        "get_credentials",
        lambda provider: {"access_token": "token", "account": "sender@example.com"},
    )

    def request(self, method, endpoint, *, params=None, json=None):
        captured.update(json or {})
        return {"id": "mail-2"}

    monkeypatch.setattr(GmailClient, "_request", request)
    GmailClient().send_email(
        to="owner@example.com",
        subject="Run report",
        body="See attached PDF.",
        attachments=[("report.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")],
    )

    raw = base64.urlsafe_b64decode(captured["raw"] + "=" * (-len(captured["raw"]) % 4))
    message = BytesParser(policy=policy.default).parsebytes(raw)
    parts = list(message.iter_attachments())
    assert len(parts) == 1
    assert parts[0].get_filename() == "report.pdf"
    assert parts[0].get_content_type() == "application/pdf"
    assert parts[0].get_payload(decode=True) == b"%PDF-1.4\n%%EOF"
