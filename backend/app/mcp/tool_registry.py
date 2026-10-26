from __future__ import annotations

import copy
from typing import Any

from app.config import settings
from app.integrations.github import GitHubClient
from app.integrations.gmail import GmailClient
from app.services.agent_run_context import active_repository_root, active_repository_slug
from app.mcp.manager import get_mcp_manager
from app.mcp.repository import RepositoryMCPClient


#: Tools that only read state. Safe for agents to run automatically.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "repo.list_files",
        "repo.search_files",
        "repo.read_file",
        "github.get_repository",
        "github.list_pull_requests",
    }
)

#: Tools that can mutate state. Repository writes and allowlisted commands are
#: confined to an isolated, unpushed run branch and shown in the final diff;
#: external-service actions are deferred to the delivery approval gate.
SIDE_EFFECTING_TOOLS: frozenset[str] = frozenset(
    {
        "repo.write_file",
        "repo.run_command",
        "gmail.send_email",
        "github.create_issue",
        "github.create_pull_request",
    }
)

# Only actions that reach a third-party service wait for the final human gate.
HUMAN_APPROVAL_TOOLS: frozenset[str] = frozenset(
    {"gmail.send_email", "github.create_issue", "github.create_pull_request"}
)

#: Which MCP server owns each tool. The MCP tool name is the part after the dot.
SERVER_FOR_TOOL: dict[str, str] = {
    "repo.list_files": "repo",
    "repo.search_files": "repo",
    "repo.read_file": "repo",
    "repo.write_file": "repo",
    "repo.run_command": "repo",
    "github.get_repository": "github",
    "github.list_pull_requests": "github",
    "github.create_issue": "github",
    "gmail.list_messages": "gmail",
    "gmail.send_email": "gmail",
}

#: LLM function-call names may not contain dots, so canonical tool names are
#: mangled when handed to the model: ``repo.list_files`` -> ``repo__list_files``.
FUNCTION_NAME_SEPARATOR = "__"

#: Values a model tends to invent when it does not know which repository to use.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset(
    {
        "",
        "placeholder",
        "owner",
        "repo",
        "repository",
        "example",
        "your-org",
        "your-repo",
        "your_org",
        "my-org",
        "my-repo",
        "unknown",
        "none",
    }
)


def default_repo_parts() -> tuple[str, str]:
    """Return the selected OAuth repo, or the configured ``GITHUB_REPO`` fallback."""
    from app.services import credential_store
    from app.services.credential_store import CredentialStoreError

    try:
        credentials = credential_store.get_credentials("github")
    except CredentialStoreError:
        credentials = None
    slug = str((credentials or {}).get("default_repo") or settings.github_repo or "").strip().strip("/")
    if "/" not in slug:
        return "", ""
    owner, _, repo = slug.partition("/")
    return owner.strip(), repo.strip()


def resolve_repo_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Force every GitHub call to the repository selected for this workspace."""
    if not tool_name.startswith("github."):
        return arguments
    run_repository = active_repository_slug()
    if run_repository and "/" in run_repository:
        owner, _, repo = run_repository.partition("/")
    else:
        owner, repo = default_repo_parts()
    if not owner or not repo:
        return arguments
    resolved = dict(arguments)
    # Do not let an agent-supplied owner/repo accidentally send a read or
    # deferred action to a different repository than the user selected.
    resolved["owner"] = owner
    resolved["repo"] = repo
    return resolved


def tool_to_function_name(tool_name: str) -> str:
    """``repo.read_file`` -> ``repo__read_file`` (LLM-safe function name)."""
    return tool_name.replace(".", FUNCTION_NAME_SEPARATOR)


def function_name_to_tool(function_name: str) -> str:
    """Inverse of :func:`tool_to_function_name`."""
    return function_name.replace(FUNCTION_NAME_SEPARATOR, ".")


def _tool_schema(
    tool_name: str,
    description: str,
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a function-calling schema (the OpenAI-style wire format LangChain and Groq use)."""
    return {
        "type": "function",
        "function": {
            "name": tool_to_function_name(tool_name),
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
            },
        },
    }


#: JSON schemas exposed to the LLM so it can decide which tools to call and
#: with what arguments. Keyed by canonical (dotted) tool name.
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "repo.list_files": _tool_schema(
        "repo.list_files",
        "List repository files under a path, excluding build and VCS directories.",
        {
            "relative_path": {"type": "string", "description": "Directory relative to the repo root. Defaults to '.'."},
            "max_files": {"type": "integer", "description": "Maximum number of files to return."},
        },
    ),
    "repo.search_files": _tool_schema(
        "repo.search_files",
        "Find repository files whose name contains a query string.",
        {
            "query": {"type": "string", "description": "Substring to match against file names."},
            "relative_path": {"type": "string", "description": "Directory relative to the repo root. Defaults to '.'."},
        },
        ("query",),
    ),
    "repo.read_file": _tool_schema(
        "repo.read_file",
        "Read a UTF-8 file from the repository and return its content.",
        {"file_path": {"type": "string", "description": "Path to the file relative to the repo root."}},
        ("file_path",),
    ),
    "repo.write_file": _tool_schema(
        "repo.write_file",
        "Write complete UTF-8 file content inside the isolated workspace, or set append=true to add a focused snippet while preserving the existing file. Large reductions to an existing file are rejected as possible truncation. Set allow_shrink=true only for intentional large removals.",
        {
            "file_path": {"type": "string", "description": "Path to the file relative to the repo root."},
            "content": {"type": "string", "description": "Full file content, or only the new snippet when append=true."},
            "allow_shrink": {"type": "boolean", "description": "Set true only when the user explicitly requested a substantial removal or replacement."},
            "append": {"type": "boolean", "description": "Set true to append only the new snippet, preserving all existing file content. Use for additive changes to existing files."},
        },
        ("file_path", "content"),
    ),
    "repo.run_command": _tool_schema(
        "repo.run_command",
        "Run one exact allowlisted project validation command. Shell syntax, command chains, and unsupported commands are rejected. For backend tests use exactly: python -m pytest backend/tests -q.",
        {"command": {"type": "string", "description": "Exact supported validation command; do not add shell syntax or chain commands."}},
        ("command",),
    ),
    "github.get_repository": _tool_schema(
        "github.get_repository",
        "Fetch repository metadata for an owner/repo pair.",
        {
            "owner": {"type": "string", "description": "Repository owner or organisation."},
            "repo": {"type": "string", "description": "Repository name."},
        },
        ("owner", "repo"),
    ),
    "github.list_pull_requests": _tool_schema(
        "github.list_pull_requests",
        "List pull requests for an owner/repo pair.",
        {
            "owner": {"type": "string", "description": "Repository owner or organisation."},
            "repo": {"type": "string", "description": "Repository name."},
            "state": {"type": "string", "description": "Either 'open', 'closed' or 'all'."},
        },
        ("owner", "repo"),
    ),
    "github.create_issue": _tool_schema(
        "github.create_issue",
        "Create a GitHub issue. Side-effecting: requires human approval.",
        {
            "owner": {"type": "string", "description": "Repository owner or organisation."},
            "repo": {"type": "string", "description": "Repository name."},
            "title": {"type": "string", "description": "Issue title."},
            "body": {"type": "string", "description": "Issue body."},
        },
        ("owner", "repo", "title", "body"),
    ),
    "gmail.list_messages": _tool_schema(
        "gmail.list_messages",
        "List recent Gmail messages matching a query.",
        {
            "query": {"type": "string", "description": "Gmail search query. May be empty."},
            "max_results": {"type": "integer", "description": "Maximum number of messages to return."},
        },
    ),
    "gmail.send_email": _tool_schema(
        "gmail.send_email",
        "Send an email from the configured account. Side-effecting: requires human approval.",
        {
            "to": {"type": "string", "description": "Recipient email address."},
            "subject": {"type": "string", "description": "Email subject."},
            "body": {"type": "string", "description": "Email body."},
            "cc": {"type": "string", "description": "Optional CC address."},
        },
        ("to", "subject", "body"),
    ),
}


def _unwrap(tool_name: str, value: Any) -> Any:
    """Map an MCP server's dict response back to the canonical tool shape.

    Only the three repo read tools return non-dicts, so the MCP servers wrap
    them and they are unwrapped here.
    """
    if not isinstance(value, dict):
        return value
    if tool_name == "repo.list_files":
        return value.get("files", [])
    if tool_name == "repo.read_file":
        return value.get("content", "")
    if tool_name == "repo.search_files":
        return value.get("matches", [])
    return value


def _apply_github_defaults(schema: dict[str, Any]) -> dict[str, Any]:
    """Advertise the configured default repository inside a GitHub tool schema.

    Without this the model has no idea which repository to inspect and calls the
    tool with placeholders; the schema both names the repository and relaxes the
    ``owner``/``repo`` requirement once a default exists.
    """
    function = schema.get("function", {})
    if not str(function.get("name", "")).startswith("github"):
        return schema
    owner, repo = default_repo_parts()
    if not owner or not repo:
        return schema

    patched = copy.deepcopy(schema)
    patched_function = patched["function"]
    parameters = patched_function["parameters"]
    properties = parameters["properties"]

    if "owner" in properties:
        properties["owner"]["description"] = f"Repository owner or organisation. Defaults to '{owner}'."
    if "repo" in properties:
        properties["repo"]["description"] = f"Repository name. Defaults to '{repo}'."

    patched_function["description"] = (
        f"{patched_function['description']} Target repository: {owner}/{repo}."
    )
    parameters["required"] = [
        item for item in parameters.get("required", []) if item not in ("owner", "repo")
    ]
    return patched


class MCPToolRegistry:
    def __init__(self, use_mcp: bool | None = None) -> None:
        self.clients = {
            "repo": RepositoryMCPClient(),
            "gmail": GmailClient(),
            "github": GitHubClient(),
        }
        self.manager = get_mcp_manager()
        self.use_mcp = settings.mcp_enabled if use_mcp is None else use_mcp

    @staticmethod
    def is_read_only(tool_name: str) -> bool:
        return tool_name in READ_ONLY_TOOLS

    @staticmethod
    def is_side_effecting(tool_name: str) -> bool:
        return tool_name in SIDE_EFFECTING_TOOLS

    @staticmethod
    def requires_human_approval(tool_name: str) -> bool:
        return tool_name in HUMAN_APPROVAL_TOOLS

    @staticmethod
    def can_run_automatically(tool_name: str, arguments: dict[str, Any]) -> bool:
        if tool_name in READ_ONLY_TOOLS:
            return True
        if tool_name == "repo.write_file":
            # A code write is automatic only inside the branch cloned for an
            # approved run. Unscoped agent calls cannot mutate the base repo.
            return active_repository_root() is not None
        if tool_name == "repo.run_command":
            return active_repository_root() is not None and RepositoryMCPClient.is_safe_validation_command(
                str(arguments.get("command") or "")
            )
        return False

    def server_for(self, tool_name: str) -> str | None:
        return SERVER_FOR_TOOL.get(tool_name)

    def get_tools_for_agent(self, agent_name: str) -> list[str]:
        mapping = {
            "planner": ["repo.list_files", "repo.search_files", "github.get_repository"],
            "developer": ["repo.read_file", "repo.write_file", "repo.list_files", "repo.search_files"],
            "reviewer": ["repo.read_file"],
            "tester": ["repo.run_command"],
            # Documentation can rely on the run evidence already in graph state;
            # reading repository files here burns the small docs tool budget and
            # has repeatedly prevented the required report write.
            "docs": ["repo.write_file", "gmail.send_email", "github.create_issue"],
        }
        return mapping.get(agent_name, [])

    def get_tool_schemas_for_agent(self, agent_name: str) -> list[dict[str, Any]]:
        """Function-calling schemas for exactly the tools this agent may use."""
        return [
            _apply_github_defaults(TOOL_SCHEMAS[tool_name])
            for tool_name in self.get_tools_for_agent(agent_name)
            if tool_name in TOOL_SCHEMAS
        ]

    def list_tool_catalog(self) -> list[dict[str, str]]:
        return [
            {"name": "repo.list_files", "description": "List repository files and project structure."},
            {"name": "repo.search_files", "description": "Search the worktree for text patterns and symbols."},
            {"name": "repo.read_file", "description": "Read file content from the repository."},
            {"name": "repo.write_file", "description": "Write or safely append file content in the repository."},
            {"name": "repo.run_command", "description": "Execute one exact supported validation command in the repo context."},
            {"name": "gmail.list_messages", "description": "List recent Gmail messages for follow-ups and outreach."},
            {"name": "gmail.send_email", "description": "Send a Gmail message using the configured account."},
            {"name": "github.get_repository", "description": "Fetch repository metadata and default branch data from GitHub."},
            {"name": "github.list_pull_requests", "description": "Inspect open or closed pull requests for a repository."},
            {"name": "github.create_issue", "description": "Create a GitHub issue for a tracked task or bug."},
            {"name": "github.create_pull_request", "description": "Publish an approved run branch as a pull request."},
        ]

    def _call_direct(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        client_name, method_name = tool_name.split(".", 1)
        client = self.clients.get(client_name)
        if client is None:
            raise ValueError(f"Unknown tool client: {client_name}")
        return getattr(client, method_name)(**arguments)

    def call_tool_with_meta(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool, reporting whether it ran over MCP or in-process."""
        arguments = resolve_repo_arguments(tool_name, arguments)
        server = self.server_for(tool_name)
        scoped_workspace_tool = tool_name.startswith("repo.") and active_repository_root() is not None
        if self.use_mcp and server is not None and not scoped_workspace_tool:
            try:
                raw = self.manager.call_tool(server, tool_name.split(".", 1)[1], arguments)
                return {"result": _unwrap(tool_name, raw), "via": "mcp", "server": server}
            except Exception as exc:  # fall back so a broken server can't break a run
                return {
                    "result": self._call_direct(tool_name, arguments),
                    "via": "direct",
                    "server": server,
                    "mcp_error": str(exc) or type(exc).__name__,
                }
        return {"result": self._call_direct(tool_name, arguments), "via": "direct", "server": server}

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        return self.call_tool_with_meta(tool_name, arguments)["result"]

    def mcp_status(self, probe: bool = True) -> dict[str, Any]:
        from app.mcp.client import SERVER_NAMES

        if not probe:
            return {"enabled": self.use_mcp, "servers": [{"server": name} for name in SERVER_NAMES]}
        return {"enabled": self.use_mcp, "servers": self.manager.all_status()}
