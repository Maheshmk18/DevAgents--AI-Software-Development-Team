from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import ApprovalDecision
from app.services.workflow_service import workflow_service

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.get("")
async def list_approvals() -> list[dict[str, Any]]:
    return workflow_service.list_approvals()


@router.get("/{approval_id}")
async def get_approval(approval_id: str) -> dict[str, Any]:
    approval = workflow_service.get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return approval


@router.post("/{approval_id}/decision")
async def decide_approval(approval_id: str, decision: ApprovalDecision) -> dict[str, Any]:
    try:
        return workflow_service.update_approval(approval_id, decision.action, decision.arguments)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
