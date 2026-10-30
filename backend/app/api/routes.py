from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.schemas import RunRequest
from app.services.connection_service import connection_status
from app.services.llm_service import llm_service
from app.services.workflow_service import workflow_service

router = APIRouter(prefix="/api", tags=["core"])


@router.get("/version")
async def version(request: Request) -> dict[str, str]:
    return {"version": request.app.version}


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "ai-dev-team", "llm": llm_service.status()}


@router.get(
    "/healthcheck",
    summary="Health check",
    description="Returns an OK status when the API process is healthy.",
)
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}

#workflow tested  api
@router.get(
    "/ping",
    summary="Ping the API",
    description='Returns the fixed response `{ "message": "pong" }` when the API is reachable.',
)
async def ping() -> dict[str, str]:
    return {"message": "pong"}


@router.get("/connections")
async def connections() -> dict[str, Any]:
    """Where each integration is configured and whether it is live (no secrets)."""
    return connection_status()


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
    return workflow_service.serialize_run(run)


@router.delete("/runs")
async def clear_runs() -> dict[str, Any]:
    """Wipe the run history (runs, approvals, evaluations) for a clean console."""
    return {"deleted": workflow_service.clear_runs()}


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str) -> dict[str, Any]:
    if not workflow_service.delete_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return {"deleted": 1, "run_id": run_id}


@router.get("/agents")
async def list_agents() -> list[str]:
    return ["planner", "developer", "reviewer", "tester", "docs"]

@router.get(
    "/hello",
    summary="Hello endpoint",
    description='Returns the fixed response `{ "message": "Agent workflow works" }`.',
)
async def hello() -> dict[str, str]:
    return {"message": "Agent workflow works"}
