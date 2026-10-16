from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.schemas import RunRequest
from app.services.llm_service import llm_service
from app.services.workflow_service import workflow_service

router = APIRouter(prefix="/api", tags=["core"])


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "ai-dev-team", "llm": llm_service.status()}


@router.get("/llm")
async def llm_status() -> dict[str, Any]:
    return llm_service.status()


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def create_run(request: RunRequest) -> dict[str, Any]:
    run_record = workflow_service.create_run(request.message)
    return run_record


@router.get("/runs")
async def list_runs() -> list[dict[str, Any]]:
    return workflow_service.list_runs()


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    run = workflow_service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/agents")
async def list_agents() -> list[str]:
    return ["planner", "developer", "reviewer", "tester", "docs"]
