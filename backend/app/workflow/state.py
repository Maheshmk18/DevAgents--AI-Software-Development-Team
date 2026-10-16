from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class DevTeamState(TypedDict, total=False):
    """Shared state that flows through every node of the dev-team graph.

    Each agent receives this state and returns only the keys it owns.
    ``stage_results`` is append-only (``operator.add``) so the full history,
    including review retries, is preserved.
    """

    run_id: str
    request: str
    plan: list[dict[str, Any]]
    stage_results: Annotated[list[dict[str, Any]], operator.add]
    tool_events: Annotated[list[dict[str, Any]], operator.add]
    current_stage: str
    review_attempts: int
    review_passed: bool
    status: str
    final_answer: str | None
    error: str | None
