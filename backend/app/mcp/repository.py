from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any

from app.config import settings
from app.services.agent_run_context import active_repository_root


_IGNORED_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    ".venv-new",
    "venv",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
}


class RepositoryMCPClient:
    # These project checks are safe for the backend to run during a workflow.
    # Commands outside this small allowlist are never interpreted by a shell.
    _SAFE_VALIDATION_COMMANDS = {
        "python -m pytest",
        "python -m pytest -q",
        "python -m pytest backend/tests -q",
        "pytest -q",
        "python -m unittest",
        "python -m compileall -q .",
        "python -m compileall -q backend/app",
        "npm run build",
        "npm test",
        "npm run test",
        "npm --prefix frontend run build",
        "npm --prefix frontend test",
        "npm --prefix frontend run test",
    }

    def __init__(self, root_path: str | None = None) -> None:
        if root_path:
            self._configured_root_path = Path(root_path)
        elif settings.repo_root:
            self._configured_root_path = Path(settings.repo_root)
        else:
            self._configured_root_path = Path.cwd()

    @property
    def root_path(self) -> Path:
        """Use the active run's isolated clone when tools execute inside a run."""
        return active_repository_root() or self._configured_root_path

    @staticmethod
    def is_safe_validation_command(command: str) -> bool:
        """Whether a command is an exact, shell-free project check we allow."""
        return " ".join((command or "").strip().split()) in RepositoryMCPClient._SAFE_VALIDATION_COMMANDS

    def _resolve_path(self, file_path: str) -> Path:
        root = self.root_path.resolve()
        target = (root / file_path).resolve()
        if target != root and root not in target.parents:
            raise ValueError("Repository paths must stay inside the workspace.")
        return target

    def list_files(self, relative_path: str = ".", max_files: int = 200) -> list[str]:
        base_path = self._resolve_path(relative_path)
        if not base_path.exists():
            return []
        files: list[str] = []
        root = self.root_path.resolve()

        def skip_unreadable(_: OSError) -> None:
            # A restricted temp clone can contain inaccessible directories.
            # Keep the files already found and continue with readable paths.
            return

        for current, dirnames, filenames in os.walk(base_path, onerror=skip_unreadable):
            dirnames[:] = [name for name in dirnames if name not in _IGNORED_DIRS]
            for filename in filenames:
                files.append(str((Path(current) / filename).relative_to(root)))
                if len(files) >= max_files:
                    return files
        return files

    def read_file(self, file_path: str) -> str:
        target = self._resolve_path(file_path)
        return target.read_text(encoding="utf-8") if target.exists() else ""

    def write_file(
        self,
        file_path: str,
        content: str,
        allow_shrink: bool = False,
        append: bool = False,
    ) -> dict[str, Any]:
        target = self._resolve_path(file_path)
        if any(part in _IGNORED_DIRS for part in target.relative_to(self.root_path.resolve()).parts):
            raise ValueError("Agent writes are not allowed inside generated or VCS directories.")
        if re.fullmatch(r"\.env(?:\..*)?", target.name, flags=re.IGNORECASE) and target.name.lower() != ".env.example":
            raise ValueError("Agent writes to environment secret files are blocked.")
        if append:
            existing = target.read_text(encoding="utf-8") if target.is_file() else ""
            if content and content in existing:
                return {
                    "file_path": file_path,
                    "bytes_written": 0,
                    "resulting_bytes": len(existing.encode("utf-8")),
                    "appended": False,
                    "already_present": True,
                }
            if not existing or existing.endswith("\n\n"):
                separator = ""
            elif existing.endswith(("\n", "\r")):
                separator = "\n"
            else:
                separator = "\n\n"
            updated = f"{existing}{separator}{content}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(updated, encoding="utf-8")
            return {
                "file_path": file_path,
                "bytes_written": len(content.encode("utf-8")),
                "resulting_bytes": len(updated.encode("utf-8")),
                "appended": True,
            }
        new_size = len(content.encode("utf-8"))
        if target.is_file():
            current_size = target.stat().st_size
            if not allow_shrink and current_size >= 700 and new_size < current_size * 0.65:
                raise ValueError(
                    "Write rejected because the proposed file is much shorter than the existing file "
                    f"({new_size} bytes versus {current_size} bytes). The response may be truncated. "
                    "Read the complete file and preserve its unrelated content, or add a focused new file."
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"file_path": file_path, "bytes_written": new_size}

    def search_files(self, query: str, relative_path: str = ".") -> list[str]:
        base_path = self._resolve_path(relative_path)
        if not base_path.exists():
            return []
        matches: list[str] = []
        root = self.root_path.resolve()

        def skip_unreadable(_: OSError) -> None:
            return

        for current, dirnames, filenames in os.walk(base_path, onerror=skip_unreadable):
            dirnames[:] = [name for name in dirnames if name not in _IGNORED_DIRS]
            for filename in filenames:
                if query.lower() in filename.lower():
                    matches.append(str((Path(current) / filename).relative_to(root)))
        return matches

    def run_command(self, command: str, timeout: int = 120) -> dict[str, Any]:
        normalized = " ".join((command or "").strip().split())
        if normalized not in self._SAFE_VALIDATION_COMMANDS:
            return {
                "command": command,
                "return_code": 126,
                "stdout": "",
                "stderr": "This command is outside the automatic validation allowlist and was not run.",
                "timed_out": False,
            }

        workspace_root = self.root_path.resolve()
        backend_root = workspace_root / "backend"
        if not (backend_root / "app").is_dir() and (workspace_root / "app").is_dir():
            backend_root = workspace_root
        run_cwd = workspace_root

        if normalized.startswith("python "):
            python_args = shlex.split(normalized)[1:]
            if normalized.startswith("python -m pytest") and (backend_root / "tests").is_dir():
                # Run backend tests with the backend as cwd so ``app`` imports
                # work even when pytest is launched for a repository-root clone.
                run_cwd = backend_root
                python_args = ["-m", "pytest", "-q"]
            elif normalized == "python -m compileall -q ." and (backend_root / "app").is_dir():
                run_cwd = backend_root
                python_args = ["-m", "compileall", "-q", "app"]
            elif normalized == "python -m compileall -q backend/app" and backend_root != workspace_root:
                run_cwd = workspace_root
                python_args = ["-m", "compileall", "-q", "backend/app"]
            args = [sys.executable, *python_args]
            use_shell = False
        elif normalized == "pytest -q":
            args = [sys.executable, "-m", "pytest", "-q"]
            if (backend_root / "tests").is_dir():
                run_cwd = backend_root
            use_shell = False
        else:
            # On Windows npm is a .cmd file. The command is an exact allowlist
            # member above, so this fixed invocation cannot contain shell input.
            if os.name == "nt":
                args = ["cmd.exe", "/d", "/s", "/c", normalized]
            else:
                args = shlex.split(normalized)
            use_shell = False

        environment = os.environ.copy()
        if (backend_root / "app").is_dir():
            python_paths = [str(backend_root)]
            existing_pythonpath = environment.get("PYTHONPATH", "")
            if existing_pythonpath:
                python_paths.append(existing_pythonpath)
            environment["PYTHONPATH"] = os.pathsep.join(python_paths)

        effective_timeout = min(max(int(timeout), 1), 60)
        try:
            result = subprocess.run(
                args,
                shell=use_shell,
                capture_output=True,
                text=True,
                cwd=str(run_cwd),
                env=environment,
                # Never inherit the caller's stdin: inside an MCP stdio server the
                # inherited handle is the JSON-RPC pipe and can deadlock the child.
                stdin=subprocess.DEVNULL,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "command": command,
                "return_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {effective_timeout}s.",
                "timed_out": True,
            }
        return {
            "command": command,
            "return_code": result.returncode,
            "stdout": result.stdout[:20000],
            "stderr": result.stderr[:20000],
            "timed_out": False,
        }
