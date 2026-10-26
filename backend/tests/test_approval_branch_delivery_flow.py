from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services import execution_service as execution_module
from app.services import workflow_service as workflow_module
from app.services.execution_service import ExecutionService
from app.integrations.github import GitHubClient
from app.services.github_workspace import GitHubWorkspaceService
from app.services.workflow_service import WorkflowService


@pytest.fixture
def isolated_git_config(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "workflow-test-gitconfig"
    config_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def trust(*directories: Path) -> None:
        for directory in directories:
            subprocess.run(
                ["git", "config", "--global", "--add", "safe.directory", str(directory.resolve())],
                check=True,
            )

    return trust


def _disable_database(monkeypatch, module=workflow_module) -> None:
    monkeypatch.setattr(module, "insert_run", lambda document: document)
    monkeypatch.setattr(module, "insert_approval", lambda document: document)
    monkeypatch.setattr(module, "db_update_approval", lambda approval_id, update: update)
    monkeypatch.setattr(module, "db_get_run", lambda run_id: None)
    monkeypatch.setattr(module, "db_get_approval", lambda approval_id: None)


def test_plan_is_shown_before_agents_or_branch_start(monkeypatch) -> None:
    _disable_database(monkeypatch)
    service = WorkflowService()
    monkeypatch.setattr(service, "_start_plan", lambda run_id: None)
    started: list[str] = []
    monkeypatch.setattr(service, "start_execution", started.append)
    monkeypatch.setattr(workflow_module, "default_repo_parts", lambda: ("acme", "widget"))
    monkeypatch.setattr(
        workflow_module.WorkflowOrchestrator,
        "plan",
        lambda self, request: {
            "summary": "Add a safe health endpoint and cover both response paths.",
            "tasks": [{"id": "api", "title": "Implement the endpoint", "owner_agent": "developer"}],
        },
    )

    run = service.create_run("Add a health endpoint")
    assert run["status"] == "running"
    assert not run["approvals"]
    assert not started

    service._prepare_plan_in_background(run["id"])
    run = service.get_run(run["id"])
    approval_id = run["plan_approval"]
    approval = service.get_approval(approval_id)
    assert run["status"] == "waiting_for_plan_approval"
    assert approval["payload"]["repository_slug"] == "acme/widget"
    assert approval["payload"]["tasks"][0]["title"] == "Implement the endpoint"
    assert not started

    service.update_approval(approval_id, "approve")
    assert started == [run["id"]]
    assert run["status"] == "approved"


def test_rejecting_plan_never_starts_branch_execution(monkeypatch) -> None:
    _disable_database(monkeypatch)
    service = WorkflowService()
    monkeypatch.setattr(service, "_start_plan", lambda run_id: None)
    started: list[str] = []
    monkeypatch.setattr(service, "start_execution", started.append)
    monkeypatch.setattr(workflow_module, "default_repo_parts", lambda: ("acme", "widget"))
    monkeypatch.setattr(
        workflow_module.WorkflowOrchestrator,
        "plan",
        lambda self, request: {"summary": "Plan", "tasks": []},
    )
    run = service.create_run("Fix the API")
    service._prepare_plan_in_background(run["id"])
    approval_id = service.get_run(run["id"])["plan_approval"]

    service.update_approval(approval_id, "reject")

    assert service.get_run(run["id"])["status"] == "rejected"
    assert started == []


def test_public_run_view_does_not_expose_local_workspace_path() -> None:
    view = WorkflowService().serialize_run({"id": "run", "workspace_root": "C:/Users/private/temp/run"})

    assert "workspace_root" not in view


def test_successful_run_starts_pr_publication_without_final_approval(monkeypatch) -> None:
    _disable_database(monkeypatch)
    service = WorkflowService()
    run_id = "a" * 32
    service.runs[run_id] = {
        "id": run_id,
        "message": "Add health endpoint",
        "status": "running",
        "repository_slug": "acme/widget",
        "workspace_root": "C:/temp/branch",
        "branch_name": "agent/add-health-endpoint-aaaaaaaa",
        "base_branch": "main",
        "approvals": [],
        "tool_events": [{
            "event_id": "branch-event",
            "agent": "backend",
            "tool": "github.prepare_branch",
            "status": "executed",
        }],
        "stage_results": [],
    }
    stages = [
        {"agent": "developer", "status": "implementation_ready", "summary": "Added the endpoint."},
        {"agent": "reviewer", "status": "review_passed", "summary": "Review passed."},
        {"agent": "tester", "status": "validated", "summary": "Checks passed."},
        {"agent": "docs", "status": "documented", "summary": "Documented the API."},
    ]
    monkeypatch.setattr(
        service.execution_service,
        "execute_run",
        lambda run, on_progress=None: {
            "run_id": run_id,
            "stage_results": stages,
            "tool_events": [{
                "event_id": "write-1",
                "agent": "developer",
                "tool": "repo.write_file",
                "status": "executed",
                "arguments": {"file_path": "app/api.py"},
            }],
            "deferred_actions": [
                {
                    "event_id": "agent-pr-1",
                    "agent": "docs",
                    "tool": "github.create_pull_request",
                    "arguments": {"title": "Agent suggested PR"},
                },
                {
                    "event_id": "email-1",
                    "agent": "docs",
                    "tool": "gmail.send_email",
                    "purpose": "final_run_report",
                    "arguments": {"to": "owner@example.com", "body": "Report"},
                },
            ],
            "final_answer": "Endpoint implemented and validated.",
            "updated_at": "now",
        },
    )
    monkeypatch.setattr(
        GitHubWorkspaceService,
        "prepare_review",
        lambda self, run_id, workspace_root, paths: {
            "changed_files": ["app/api.py"],
            "diff": "diff --git a/app/api.py b/app/api.py\n+health endpoint\n",
            "diff_truncated": False,
        },
    )
    monkeypatch.setattr(
        workflow_module,
        "write_run_documentation_pdf",
        lambda workspace_root, run, written_paths: "docs/ai-dev-team-run-aaaaaaaa.pdf",
    )

    scheduled: list[tuple[str, dict, list[dict]]] = []
    monkeypatch.setattr(
        service,
        "_start_pr_publication",
        lambda current_run_id, pull_request, additional: scheduled.append(
            (current_run_id, pull_request, additional)
        ),
    )

    service.execute_run(run_id)

    run = service.get_run(run_id)
    assert run["status"] == "delivering"
    assert run.get("action_approval") is None
    assert len(scheduled) == 1
    current_run_id, pull_request, additional = scheduled[0]
    assert current_run_id == run_id
    assert pull_request["tool"] == "github.create_pull_request"
    assert sum(
        action["tool"] == "github.create_pull_request"
        for action in run["deferred_actions"]
    ) == 1
    assert [item["tool"] for item in additional] == ["gmail.send_email"]
    assert additional[0]["arguments"]["attachment_path"] == "docs/ai-dev-team-run-aaaaaaaa.pdf"
    assert run["workspace_review"]["diff"].startswith("diff --git")
    assert run["tool_events"][0]["tool"] == "github.prepare_branch"
    event_statuses = {
        event["tool"]: event["status"]
        for event in run["tool_events"]
        if event["tool"] in {"github.create_pull_request", "gmail.send_email"}
    }
    assert event_statuses == {
        "github.create_pull_request": "running",
        "gmail.send_email": "queued",
    }


def test_created_pr_is_emailed_with_report_before_optional_action_approval(monkeypatch) -> None:
    _disable_database(monkeypatch)
    service = WorkflowService()
    run_id = "e" * 32
    service.runs[run_id] = {
        "id": run_id,
        "status": "delivering",
        "approvals": [],
        "deferred_actions": [],
        "tool_events": [],
        "action_results": [],
    }
    pull_request = {
        "event_id": "pr",
        "agent": "backend",
        "tool": "github.create_pull_request",
        "purpose": "publish_run_pull_request",
        "arguments": {},
    }
    email = {
        "event_id": "mail",
        "agent": "docs",
        "tool": "gmail.send_email",
        "purpose": "final_run_report",
        "arguments": {"to": "owner@example.com", "body": "Report: {{PULL_REQUEST_URL}}"},
    }

    calls: list[list[dict]] = []

    def publish(run_id, actions, repository_work_failed, awaiting_pr_review=False):
        run = service.get_run(run_id)
        assert repository_work_failed is False
        assert awaiting_pr_review is True
        calls.append(actions)
        if actions == [pull_request]:
            run["pull_request_url"] = "https://github.com/acme/widget/pull/42"
            run["status"] = "waiting_for_pr_review"
            run["action_results"] = [{
                "event_id": "pr",
                "tool": "github.create_pull_request",
                "status": "executed",
            }]
        else:
            assert actions == [email]
            assert actions[0]["arguments"]["body"].endswith(
                "https://github.com/acme/widget/pull/42"
            )
            run["status"] = "waiting_for_pr_review"
            run["action_results"].append({
                "event_id": "mail",
                "tool": "gmail.send_email",
                "status": "executed",
            })

    monkeypatch.setattr(service, "_execute_approved_actions", publish)

    service._publish_pr_then_queue_actions(run_id, pull_request, [email])

    run = service.get_run(run_id)
    assert calls == [[pull_request], [email]]
    assert run["status"] == "waiting_for_pr_review"
    assert run["pull_request_url"] == "https://github.com/acme/widget/pull/42"
    assert run.get("action_approval") is None
    assert [result["tool"] for result in run["action_results"]] == [
        "github.create_pull_request",
        "gmail.send_email",
    ]


def test_created_pr_without_extra_actions_waits_for_github_review(monkeypatch) -> None:
    _disable_database(monkeypatch)
    service = WorkflowService()
    run_id = "f" * 32
    service.runs[run_id] = {
        "id": run_id,
        "status": "delivering",
        "approvals": [],
        "tool_events": [],
    }
    pull_request = {"event_id": "pr", "tool": "github.create_pull_request"}

    def publish(run_id, actions, repository_work_failed, awaiting_pr_review=False):
        run = service.get_run(run_id)
        run["pull_request_url"] = "https://github.com/acme/widget/pull/43"
        run["status"] = "waiting_for_pr_review"

    monkeypatch.setattr(service, "_execute_approved_actions", publish)

    service._publish_pr_then_queue_actions(run_id, pull_request, [])

    run = service.get_run(run_id)
    assert run["status"] == "waiting_for_pr_review"
    assert run["pull_request_url"].endswith("/pull/43")
    assert run.get("action_approval") is None
    assert service.approvals == {}


def test_failed_pr_does_not_queue_optional_actions(monkeypatch) -> None:
    _disable_database(monkeypatch)
    service = WorkflowService()
    run_id = "9" * 32
    service.runs[run_id] = {
        "id": run_id,
        "status": "delivering",
        "approvals": [],
        "tool_events": [],
        "action_results": [],
    }
    pull_request = {"event_id": "pr", "tool": "github.create_pull_request"}
    email = {"event_id": "mail", "tool": "gmail.send_email", "arguments": {}}

    def fail_publish(run_id, actions, repository_work_failed, awaiting_pr_review=False):
        run = service.get_run(run_id)
        run["status"] = "completed_with_errors"
        run["error"] = "Delivery finished with issues: GitHub rejected the branch push."
        run["action_results"] = [{
            "event_id": "pr",
            "tool": "github.create_pull_request",
            "status": "error",
            "error": "GitHub rejected the branch push.",
        }]

    monkeypatch.setattr(service, "_execute_approved_actions", fail_publish)

    service._publish_pr_then_queue_actions(run_id, pull_request, [email])

    run = service.get_run(run_id)
    assert run["status"] == "completed_with_errors"
    assert "not run because the pull request was not created" in run["action_results"][-1]["error"]
    assert service.approvals == {}


def test_delivery_creates_pr_then_sends_report_with_pr_url(monkeypatch) -> None:
    service = ExecutionService()
    assert service.registry.is_side_effecting("github.create_pull_request")
    assert service.registry.requires_human_approval("github.create_pull_request")
    side_effect_order: list[str] = []
    sent_messages: list[dict] = []

    class FakeRegistry:
        def call_tool_with_meta(self, tool, arguments):
            side_effect_order.append(tool)
            sent_messages.append(arguments)
            return {"result": {"message_id": "mail-1"}, "via": "direct"}

    service.registry = FakeRegistry()
    monkeypatch.setattr(execution_module, "gmail_report_recipient", lambda: "owner@example.com")

    def create_pr(self, run):
        side_effect_order.append("pull_request")
        return {"url": "https://github.com/acme/widget/pull/42", "number": 42}

    monkeypatch.setattr(GitHubWorkspaceService, "publish_pull_request", create_pr)
    actions = [
        {
            "event_id": "pr-1",
            "agent": "backend",
            "tool": "github.create_pull_request",
            "purpose": "publish_run_pull_request",
            "arguments": {"run_id": "a" * 32},
        },
        {
            "event_id": "mail-1",
            "agent": "docs",
            "tool": "gmail.send_email",
            "purpose": "final_run_report",
            "arguments": {"to": "owner@example.com", "body": "Report: {{PULL_REQUEST_URL}}"},
        },
    ]

    results = service.execute_deferred(actions)

    assert side_effect_order == ["pull_request", "gmail.send_email"]
    assert "https://github.com/acme/widget/pull/42" in sent_messages[0]["body"]
    assert "{{PULL_REQUEST_URL}}" not in sent_messages[0]["body"]
    assert [result["status"] for result in results] == ["executed", "executed"]


def test_failed_pr_blocks_report_email(monkeypatch) -> None:
    service = ExecutionService()
    sent: list[str] = []

    class FakeRegistry:
        def call_tool_with_meta(self, tool, arguments):
            sent.append(tool)
            return {"result": {}, "via": "direct"}

    service.registry = FakeRegistry()
    monkeypatch.setattr(execution_module, "gmail_report_recipient", lambda: "owner@example.com")

    def fail_pr(self, run):
        raise RuntimeError("GitHub rejected the branch push")

    monkeypatch.setattr(GitHubWorkspaceService, "publish_pull_request", fail_pr)
    results = service.execute_deferred([
        {"event_id": "pr-2", "tool": "github.create_pull_request", "purpose": "publish_run_pull_request", "arguments": {}},
        {"event_id": "mail-2", "tool": "gmail.send_email", "purpose": "final_run_report", "arguments": {"to": "owner@example.com", "body": "Report"}},
    ])

    assert results[0]["status"] == "error"
    assert results[1]["status"] == "blocked"
    assert sent == []


def test_rejecting_optional_action_keeps_open_pr_waiting_for_review(monkeypatch) -> None:
    _disable_database(monkeypatch)
    service = WorkflowService()
    run_id = "c" * 32
    service.runs[run_id] = {
        "id": run_id,
        "status": "waiting_for_action_approval",
        "pull_request_url": "https://github.com/acme/widget/pull/42",
        "approvals": [],
        "deferred_actions": [
            {"event_id": "pr", "tool": "github.create_pull_request", "arguments": {}},
            {"event_id": "mail", "tool": "gmail.send_email", "arguments": {}},
        ],
        "tool_events": [],
        "stage_results": [],
    }
    approval = service.create_approval(
        run_id,
        "execute_deferred_actions",
        {"actions": [{"event_id": "mail", "tool": "gmail.send_email", "arguments": {}}]},
    )

    service.update_approval(approval["id"], "reject")

    run = service.get_run(run_id)
    assert run["status"] == "waiting_for_pr_review"
    assert [result["event_id"] for result in run["action_results"]] == ["mail"]
    assert [result["status"] for result in run["action_results"]] == ["skipped"]


def test_approved_delivery_commits_pushes_selected_branch_and_opens_pr_locally(
    tmp_path: Path, monkeypatch, isolated_git_config
) -> None:
    run_id = "d" * 32
    branch = f"agent/add-endpoint-{run_id[:8]}"
    service = GitHubWorkspaceService()
    service.storage_root = tmp_path / "workspaces"
    root = service.storage_root / run_id
    root.mkdir(parents=True)
    remote = tmp_path / "selected-repo.git"
    isolated_git_config(root, remote)
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    (root / "api.py").write_text("status = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "api.py"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "--quiet", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(root), "switch", "--create", branch], check=True, capture_output=True, text=True)
    (root / "api.py").write_text("status = 'healthy'\n", encoding="utf-8")
    review = service.prepare_review(run_id, str(root), ["api.py"])
    created_pr: list[dict] = []
    monkeypatch.setattr(GitHubClient, "access_token", lambda self: "test-token")
    monkeypatch.setattr(
        GitHubClient,
        "create_pull_request",
        lambda self, **arguments: created_pr.append(arguments) or {
            "number": 42,
            "url": "https://github.com/acme/widget/pull/42",
            "status": "created",
        },
    )

    result = service.publish_pull_request({
        "id": run_id,
        "message": "Add a health endpoint",
        "repository_slug": "acme/widget",
        "workspace_root": str(root),
        "branch_name": branch,
        "base_branch": "main",
        "workspace_review": review,
        "final_answer": "Endpoint added and validated.",
        "stage_results": [],
    })

    pushed = subprocess.run(
        ["git", "--git-dir", str(remote), "show-ref", f"refs/heads/{branch}"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result["url"] == "https://github.com/acme/widget/pull/42"
    assert pushed.stdout.strip().endswith(f"refs/heads/{branch}")
    assert created_pr[0]["owner"] == "acme"
    assert created_pr[0]["repo"] == "widget"
    assert created_pr[0]["head"] == branch
    assert created_pr[0]["base"] == "main"


def test_review_stages_all_code_changes_but_never_env_files(
    tmp_path: Path, isolated_git_config
) -> None:
    run_id = "b" * 32
    service = GitHubWorkspaceService()
    service.storage_root = tmp_path / "workspaces"
    root = service.storage_root / run_id
    (root / "src").mkdir(parents=True)
    (root / "src" / "api.py").write_text("value = 1\n", encoding="utf-8")
    isolated_git_config(root)
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "src/api.py"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--quiet", "-m", "base"],
        check=True,
    )
    (root / "src" / "api.py").write_text("value = 2\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "api.md").write_text("API notes\n", encoding="utf-8")
    (root / ".env.local").write_text("SECRET=do-not-stage\n", encoding="utf-8")

    review = service.prepare_review(run_id, str(root), ["src/api.py"])

    assert set(review["changed_files"]) == {"src/api.py", "docs/api.md"}
    assert "value = 2" in review["diff"]
    assert "SECRET=do-not-stage" not in review["diff"]
