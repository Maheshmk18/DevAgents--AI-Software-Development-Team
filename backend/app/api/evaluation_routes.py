from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.workflow_service import workflow_service

router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])


class EvaluationRequest(BaseModel):
    run_id: str = Field(..., min_length=1)


@router.get("")
async def list_evaluations(request: Request) -> list[dict[str, Any]]:
    return workflow_service.list_evaluations(str(request.state.user["sub"]))


@router.post("")
async def evaluate_run(request: EvaluationRequest, http_request: Request) -> dict[str, Any]:
    try:
        return workflow_service.evaluate_run(request.run_id, str(http_request.state.user["sub"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
