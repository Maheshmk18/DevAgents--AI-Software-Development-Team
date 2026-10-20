from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.integrations.errors import IntegrationError
from app.mcp.tool_registry import MCPToolRegistry

router = APIRouter(prefix="/api/tools", tags=["tools"])
tool_registry = MCPToolRegistry()


def _fail(exc: Exception) -> HTTPException:
    """Turn an integration failure into a readable HTTP error."""
    if isinstance(exc, IntegrationError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=f"Tool failed: {exc}")


class GmailListRequest(BaseModel):
    query: str = Field(default="")
    max_results: int = Field(default=5, ge=1, le=10)


class GmailSendRequest(BaseModel):
    to: str = Field(..., min_length=1)
    subject: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)
    cc: str | None = None


class GitHubRepoRequest(BaseModel):
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)


class GitHubIssueRequest(GitHubRepoRequest):
    title: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)


@router.get("")
async def list_tools() -> list[dict[str, str]]:
    return tool_registry.list_tool_catalog()


@router.post("/gmail/messages")
async def list_gmail_messages(request: GmailListRequest) -> dict[str, Any]:
    try:
        return tool_registry.call_tool("gmail.list_messages", {"query": request.query, "max_results": request.max_results})
    except Exception as exc:
        raise _fail(exc) from exc


@router.post("/gmail/send")
async def send_gmail_message(request: GmailSendRequest) -> dict[str, Any]:
    try:
        return tool_registry.call_tool(
            "gmail.send_email",
            {
                "to": request.to,
                "subject": request.subject,
                "body": request.body,
                "cc": request.cc,
            },
        )
    except Exception as exc:
        raise _fail(exc) from exc


@router.post("/github/repository")
async def get_github_repository(request: GitHubRepoRequest) -> dict[str, Any]:
    try:
        return tool_registry.call_tool("github.get_repository", {"owner": request.owner, "repo": request.repo})
    except Exception as exc:
        raise _fail(exc) from exc


@router.post("/github/issues")
async def create_github_issue(request: GitHubIssueRequest) -> dict[str, Any]:
    try:
        return tool_registry.call_tool(
            "github.create_issue",
            {"owner": request.owner, "repo": request.repo, "title": request.title, "body": request.body},
        )
    except Exception as exc:
        raise _fail(exc) from exc
