from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class RunModel:
    run_id: str
    message: str
    status: str = "ready"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tasks: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str | None = None
    error: str | None = None
