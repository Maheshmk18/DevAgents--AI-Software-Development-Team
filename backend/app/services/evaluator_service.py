from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db import insert_evaluation


class EvaluatorService:
    def evaluate_run(self, run: dict[str, Any]) -> dict[str, Any]:
        score = 0
        checks: list[dict[str, Any]] = []

        message = str(run.get("message", "")).strip()
        if message:
            score += 20
            checks.append({"name": "request_present", "passed": True, "score": 20})
        else:
            checks.append({"name": "request_present", "passed": False, "score": 0})

        tasks = run.get("tasks") or []
        if isinstance(tasks, list) and tasks:
            score += 25
            checks.append({"name": "workflow_tasks_created", "passed": True, "score": 25})
        else:
            checks.append({"name": "workflow_tasks_created", "passed": False, "score": 0})

        approvals = run.get("approvals") or []
        if approvals:
            score += 25
            checks.append({"name": "human_approval_present", "passed": True, "score": 25})
        else:
            checks.append({"name": "human_approval_present", "passed": False, "score": 0})

        status = str(run.get("status", "")).lower()
        if status in {
            "waiting_for_human_approval",
            "waiting_for_action_approval",
            "waiting_for_pr_review",
            "ready_for_review",
            "completed",
            "approved",
            "running",
        }:
            score += 20
            checks.append({"name": "workflow_status_valid", "passed": True, "score": 20})
        else:
            checks.append({"name": "workflow_status_valid", "passed": False, "score": 0})

        if score >= 85:
            verdict = "excellent"
        elif score >= 70:
            verdict = "good"
        elif score >= 50:
            verdict = "fair"
        else:
            verdict = "needs_attention"

        result = {
            "run_id": run.get("id"),
            "score": min(score, 100),
            "verdict": verdict,
            "checks": checks,
            "summary": self._summarize(verdict, checks),
            "created_at": datetime.now(timezone.utc),
        }
        insert_evaluation(result)
        return result

    def _summarize(self, verdict: str, checks: list[dict[str, Any]]) -> str:
        if verdict == "excellent":
            return "The run is well structured with a clear request, engineering tasks, and approval boundary."
        if verdict == "good":
            return "The run is mostly complete, but a few workflow details should be tightened before production use."
        if verdict == "fair":
            return "The run is functional but still missing important quality and safety signals."
        return "The run needs more workflow completeness before it can be trusted for autonomous execution."
