from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from app.integrations.github import GitHubClient
from app.mcp.tool_registry import default_repo_parts


_REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_REVIEW_DIFF_CHARS = 30_000


class GitHubWorkspaceService:
    """Create an isolated branch worktree and publish its approved changes."""

    def __init__(self) -> None:
        self.storage_root = Path(tempfile.gettempdir()).resolve() / "ai-dev-team-workspaces"

    def selected_repository(self) -> tuple[str, str]:
        owner, repo = default_repo_parts()
        slug = f"{owner}/{repo}" if owner and repo else ""
        if not slug or not _REPO_SLUG.fullmatch(slug):
            raise RuntimeError(
                "Connect GitHub and select one repository in Integrations before starting a code workflow."
            )
        return slug, self._repository_default_branch(slug)

    def prepare_workspace(
        self,
        run_id: str,
        repository_slug: str,
        request: str,
    ) -> dict[str, str]:
        root = self._run_workspace_path(run_id)
        if root.exists():
            raise RuntimeError(
                "A workspace for this run already exists. This run cannot safely start a second branch."
            )
        if not _REPO_SLUG.fullmatch(repository_slug):
            raise RuntimeError("The selected GitHub repository is invalid. Choose it again in Integrations.")

        branch, base_branch = self._branch_names(run_id, request, repository_slug)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        token = GitHubClient().access_token()
        if not token:
            raise RuntimeError("Reconnect GitHub before starting work on the selected repository.")
        clone_url = f"https://x-access-token@github.com/{repository_slug}.git"
        self._git(
            ["clone", "--quiet", "--no-tags", "--depth", "1", "--branch", base_branch, "--", clone_url, str(root)],
            token=token,
            timeout=90,
        )
        self._git(["switch", "--create", branch], cwd=root)
        return {
            "workspace_root": str(root),
            "repository_slug": repository_slug,
            "branch_name": branch,
            "base_branch": base_branch,
        }

    def prepare_review(self, run_id: str, workspace_root: str, written_paths: list[str]) -> dict[str, Any]:
        root = self._validate_workspace_path(run_id, workspace_root)
        # Collect tool-written paths and any tracked/untracked files produced by
        # allowed validation commands. The isolated clone starts clean, so these
        # are all run changes; sensitive env files and .git stay excluded.
        status = self._git(["status", "--porcelain", "--untracked-files=all"], cwd=root)
        discovered_paths = []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            path = line[2:].lstrip()
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[1]
            discovered_paths.append(path)
        safe_paths = self._validated_written_paths([*written_paths, *discovered_paths])
        if not safe_paths:
            raise RuntimeError("The agents did not produce any repository file changes to review.")
        self._git(["add", "--", *safe_paths], cwd=root)
        changed_files = self._git(
            ["diff", "--cached", "--name-only", "--diff-filter=ACDMRT"], cwd=root
        ).splitlines()
        diff = self._git(
            ["diff", "--cached", "--no-ext-diff", "--unified=3"], cwd=root
        )
        if not changed_files or not diff.strip():
            raise RuntimeError("The implementation left no changes in the selected repository branch.")
        return {
            "changed_files": changed_files,
            "diff": diff[:_MAX_REVIEW_DIFF_CHARS],
            "diff_truncated": len(diff) > _MAX_REVIEW_DIFF_CHARS,
        }

    def publish_pull_request(self, run: dict[str, Any]) -> dict[str, Any]:
        run_id = str(run.get("id") or "")
        repository_slug = str(run.get("repository_slug") or "")
        branch = str(run.get("branch_name") or "")
        base_branch = str(run.get("base_branch") or "")
        root = self._validate_workspace_path(run_id, str(run.get("workspace_root") or ""))
        if not _REPO_SLUG.fullmatch(repository_slug) or not branch or not base_branch:
            raise RuntimeError("The approved repository branch details are missing. Start a new workflow.")

        token = GitHubClient().access_token()
        staged_diff = self._git(["diff", "--cached", "--no-ext-diff"], cwd=root)
        if not staged_diff.strip():
            raise RuntimeError("The reviewed changes are no longer staged; nothing was published.")

        title = self._pull_request_title(str(run.get("message") or ""))
        self._git(
            [
                "-c", "user.name=AI Dev Team",
                "-c", "user.email=ai-dev-team@users.noreply.github.com",
                "commit", "-m", title,
            ],
            cwd=root,
        )
        try:
            self._git(["push", "--set-upstream", "origin", branch], cwd=root, token=token, timeout=180)
        except RuntimeError as exc:
            detail = str(exc)
            lowered = detail.casefold()
            if "403" in detail and ("permission" in lowered or "denied" in lowered):
                raise RuntimeError(
                    f"GitHub denied branch write access to {repository_slug} for the connected account. "
                    "Reconnect GitHub in Integrations with an account that can push to this repository, "
                    "or select a repository where that account has write access. "
                    f"GitHub said: {detail[:900]}"
                ) from exc
            raise

        owner, repo = repository_slug.split("/", 1)
        stage_results = run.get("stage_results") or []
        stage_summaries = [
            f"- {stage.get('agent', 'Agent')}: {stage.get('summary', '')}"
            for stage in stage_results
            if isinstance(stage, dict)
        ]
        changed_files = (run.get("workspace_review") or {}).get("changed_files", [])
        body = (
            "## Request\n"
            f"{str(run.get('message') or '').strip()}\n\n"
            "## Engineering summary\n"
            f"{str(run.get('final_answer') or '').strip()}\n\n"
            "## Changed files\n"
            + ("\n".join(f"- `{path}`" for path in changed_files) or "- See this branch's diff.")
            + "\n\n## Agent review\n"
            + ("\n".join(stage_summaries) or "- Review and validation completed.")
            + "\n\nCreated from an approved AI Dev Team workflow."
        )[:18_000]
        pull_request = GitHubClient().create_pull_request(
            owner=owner,
            repo=repo,
            head=branch,
            base=base_branch,
            title=title,
            body=body,
        )
        return {
            "repository": repository_slug,
            "branch": branch,
            "base": base_branch,
            "commit_sha": self._git(["rev-parse", "HEAD"], cwd=root),
            **pull_request,
        }

    def _repository_default_branch(self, repository_slug: str) -> str:
        owner, repo = repository_slug.split("/", 1)
        metadata = GitHubClient().get_repository(owner, repo)
        permissions = metadata.get("permissions")
        if isinstance(permissions, dict) and permissions.get("push") is False:
            raise RuntimeError(
                f"The connected GitHub account can read {repository_slug} but cannot push changes to it. "
                "Reconnect GitHub in Integrations with an account that has write access, or select a writable repository."
            )
        default_branch = str(metadata.get("default_branch") or "main").strip()
        if not default_branch or default_branch.startswith("-"):
            raise RuntimeError("GitHub did not return a valid default branch for the selected repository.")
        return default_branch

    def _branch_names(self, run_id: str, request: str, repository_slug: str) -> tuple[str, str]:
        base_branch = self._repository_default_branch(repository_slug)
        summary = re.sub(r"[^a-z0-9]+", "-", request.casefold()).strip("-")[:36].strip("-")
        branch_slug = summary or "engineering-change"
        return f"agent/{branch_slug}-{run_id[:8]}", base_branch

    def _run_workspace_path(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid run identifier for the isolated repository workspace.")
        return self.storage_root / run_id

    def _validate_workspace_path(self, run_id: str, workspace_root: str) -> Path:
        expected = self._run_workspace_path(run_id)
        actual = Path(workspace_root).resolve()
        if actual != expected.resolve() or not (actual / ".git").is_dir():
            raise RuntimeError("The isolated repository workspace is missing or does not match this run.")
        return actual

    @staticmethod
    def _validated_written_paths(paths: list[str]) -> list[str]:
        result: list[str] = []
        for raw_path in paths:
            normalized = str(raw_path or "").replace("\\", "/").strip()
            if (
                not normalized
                or normalized.startswith("/")
                or re.match(r"^[A-Za-z]:", normalized)
                or any(part in {"", ".", "..", ".git"} for part in normalized.split("/"))
                or re.fullmatch(r"\.env(?:\..*)?", normalized.rsplit("/", 1)[-1], re.IGNORECASE)
            ):
                continue
            if normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _pull_request_title(request: str) -> str:
        title = " ".join(request.splitlines()[0].split())[:100].strip()
        if not title:
            return "Automated engineering change"
        if not re.match(r"^(fix|feat|docs|refactor|test|chore)(\(.+\))?:", title, re.IGNORECASE):
            title = f"fix: {title}"
        return title[:120]

    @staticmethod
    def _git(
        arguments: list[str],
        *,
        cwd: Path | None = None,
        token: str | None = None,
        timeout: int = 45,
    ) -> str:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GCM_INTERACTIVE"] = "Never"
        if token:
            with tempfile.TemporaryDirectory(prefix="ai-dev-git-auth-") as auth_directory:
                askpass = Path(auth_directory) / ("git-askpass.cmd" if os.name == "nt" else "git-askpass.sh")
                if os.name == "nt":
                    askpass.write_text(
                        '@echo off\r\necho %AI_DEV_GITHUB_TOKEN%\r\n',
                        encoding="utf-8",
                    )
                else:
                    askpass.write_text(
                        "#!/bin/sh\nprintf '%s\\n' \"$AI_DEV_GITHUB_TOKEN\"\n",
                        encoding="utf-8",
                    )
                    askpass.chmod(0o700)
                environment["AI_DEV_GITHUB_TOKEN"] = token
                environment["GIT_ASKPASS"] = str(askpass)
                environment["GIT_CONFIG_COUNT"] = "1"
                environment["GIT_CONFIG_KEY_0"] = "credential.helper"
                environment["GIT_CONFIG_VALUE_0"] = ""
                return GitHubWorkspaceService._run_git(arguments, cwd, environment, timeout)
        return GitHubWorkspaceService._run_git(arguments, cwd, environment, timeout)

    @staticmethod
    def _run_git(arguments: list[str], cwd: Path | None, environment: dict[str, str], timeout: int) -> str:
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=str(cwd) if cwd else None,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Git is not installed or is not available on the backend PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Git {arguments[0]} timed out after {timeout} seconds.") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "Git command failed.").strip()
            raise RuntimeError(f"Git {arguments[0]} failed: {detail[:1200]}")
        return result.stdout.strip()
