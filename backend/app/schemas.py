from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RunRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)

    @field_validator("message", mode="before")
    @classmethod
    def strip_message(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class TaskCreate(BaseModel):
    title: str
    description: str
    priority: Literal["low", "medium", "high"] = "medium"
    owner_agent: str = "planner"
    dependencies: list[str] = Field(default_factory=list)


class TaskRecord(TaskCreate):
    id: str
    status: Literal["pending", "in_progress", "completed", "blocked"] = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApprovalDecision(BaseModel):
    action: Literal["approve", "reject", "edit"]
    arguments: dict[str, Any] | None = None


class RunRecord(BaseModel):
    id: str
    message: str
    status: str
    created_at: datetime
    updated_at: datetime
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    final_answer: str | None = None
    error: str | None = None
