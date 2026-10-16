from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


class ApprovalService:
    def __init__(self) -> None:
        self.approvals: dict[str, dict[str, Any]] = {}

    def create_approval(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        approval_id = uuid4().hex
        approval = {
            "id": approval_id,
            "run_id": run_id,
            "action": action,
            "payload": payload or {},
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        }
        self.approvals[approval_id] = approval
        return approval

    def update_approval(self, approval_id: str, decision: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        approval = self.approvals.get(approval_id)
        if approval is None:
            raise KeyError("Approval not found")
        approval["status"] = decision
        approval["arguments"] = arguments or {}
        approval["updated_at"] = datetime.now(timezone.utc)
        return approval
