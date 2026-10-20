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

    def update_approval(
        self,
        approval_id: str,
        decision: str,
        arguments: dict[str, Any] | None = None,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a human decision and return the updated approval.

        ``record`` lets a caller decide an approval it loaded from storage (for
        example after a backend restart, when this object's in-memory map is
        empty again) instead of only approvals this process happened to create.
        Without it, restarting the backend made every stored gate undecidable.
        """
        approval = record if record is not None else self.approvals.get(approval_id)
        if approval is None:
            raise KeyError("Approval not found")
        approval["status"] = decision
        approval["arguments"] = arguments or {}
        approval["updated_at"] = datetime.now(timezone.utc)
        self.approvals[approval_id] = approval
        return approval
