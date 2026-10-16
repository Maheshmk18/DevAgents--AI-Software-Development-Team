from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import settings


_IGNORED_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
}


class RepositoryMCPClient:
    def __init__(self, root_path: str | None = None) -> None:
        if root_path:
            self.root_path = Path(root_path)
        elif settings.repo_root:
            self.root_path = Path(settings.repo_root)
        else:
            self.root_path = Path.cwd()

    def list_files(self, relative_path: str = ".", max_files: int = 200) -> list[str]:
        base_path = (self.root_path / relative_path).resolve()
        if not base_path.exists():
            return []
        files: list[str] = []
        for path in base_path.rglob("*"):
            if any(part in _IGNORED_DIRS for part in path.parts):
                continue
            if path.is_file():
                files.append(str(path.relative_to(self.root_path)))
                if len(files) >= max_files:
                    break
        return files

    def read_file(self, file_path: str) -> str:
        target = (self.root_path / file_path).resolve()
        return target.read_text(encoding="utf-8") if target.exists() else ""

    def write_file(self, file_path: str, content: str) -> dict[str, Any]:
        target = (self.root_path / file_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"file_path": file_path, "bytes_written": len(content.encode("utf-8"))}

    def search_files(self, query: str, relative_path: str = ".") -> list[str]:
        base_path = (self.root_path / relative_path).resolve()
        if not base_path.exists():
            return []
        matches: list[str] = []
        for file_path in base_path.rglob("*"):
            if file_path.is_file() and query.lower() in file_path.name.lower():
                matches.append(str(file_path.relative_to(self.root_path)))
        return matches

    def run_command(self, command: str, timeout: int = 120) -> dict[str, Any]:
        import subprocess

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(self.root_path),
                # Never inherit the caller's stdin: inside an MCP stdio server the
                # inherited handle is the JSON-RPC pipe and can deadlock the child.
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "command": command,
                "return_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s.",
                "timed_out": True,
            }
        return {
            "command": command,
            "return_code": result.returncode,
            "stdout": result.stdout[:20000],
            "stderr": result.stderr[:20000],
            "timed_out": False,
        }
