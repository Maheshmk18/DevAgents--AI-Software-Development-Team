from __future__ import annotations

from functools import lru_cache
from typing import Any, Callable

from app.agents.base import last_stage_result
from langgraph.graph import END, START, StateGraph

from app.core.agent_registry import create_agent
from app.services.llm_service import llm_service
from app.workflow.orchestrator import STAGE_ORDER
from app.workflow.state import DevTeamState

#: How many times the reviewer may send work back to the developer before the
#: run is allowed to continue to validation. Keeps the loop bounded.
MAX_REVIEW_ATTEMPTS = 2
MAX_VALIDATION_ATTEMPTS = 2


def _agent_node(agent_name: str) -> Callable[[DevTeamState], dict[str, Any]]:
    """Wrap an agent class as a LangGraph node function."""

    def node(state: DevTeamState) -> dict[str, Any]:
        return create_agent(agent_name).run(state)

    node.__name__ = f"{agent_name}_node"
    return node


def route_after_review(state: DevTeamState) -> str:
    """Conditional edge: loop back to the developer or continue to validation."""
    if state.get("review_passed", True):
        return "tester"
    if state.get("review_attempts", 0) >= MAX_REVIEW_ATTEMPTS:
        return "review_halt"
    return "developer"


def route_after_tester(state: DevTeamState) -> str:
    """Give the developer one bounded repair pass when validation fails."""
    tester = last_stage_result(state, "tester") or {}
    if tester.get("status") == "validated":
        return "docs"
    if (tester.get("details") or {}).get("repairable") is False:
        return "validation_halt"

    attempts = sum(
        1 for stage in state.get("stage_results", [])
        if stage.get("agent") == "tester"
    )
    return "developer" if attempts < MAX_VALIDATION_ATTEMPTS else "validation_halt"


def route_after_developer(state: DevTeamState) -> str:
    """Do not spend a review call when the developer produced no change."""
    developer = last_stage_result(state, "developer")
    if developer and developer.get("status") == "implementation_ready":
        return "reviewer"
    return "implementation_halt"


def implementation_halt_node(state: DevTeamState) -> dict[str, Any]:
    developer = last_stage_result(state, "developer") or {}
    summary = str(
        developer.get("summary") or "The developer did not produce a repository change."
    )
    return {
        "current_stage": "implementation_halt",
        "status": "failed",
        "final_answer": f"Workflow stopped after implementation because no reviewable change was produced. {summary}",
    }


def review_halt_node(state: DevTeamState) -> dict[str, Any]:
    reviewer = last_stage_result(state, "reviewer") or {}
    summary = str(reviewer.get("summary") or "The reviewer did not approve the changes.")
    return {
        "current_stage": "review_halt",
        "status": "failed",
        "final_answer": (
            f"Workflow stopped after {MAX_REVIEW_ATTEMPTS} review attempts. "
            f"{summary} No validation, documentation, or PR was produced."
        ),
    }


def validation_halt_node(state: DevTeamState) -> dict[str, Any]:
    tester = last_stage_result(state, "tester") or {}
    summary = str(tester.get("summary") or "The validation checks did not pass.")
    return {
        "current_stage": "validation_halt",
        "status": "failed",
        "final_answer": (
            f"Workflow stopped after {MAX_VALIDATION_ATTEMPTS} validation attempts. "
            f"{summary} No documentation, PR, or Gmail report was created."
        ),
    }


def finalize_node(state: DevTeamState) -> dict[str, Any]:
    """System node: writes the final answer and closes the run."""
    request = state.get("request", "")
    stage_results = list(state.get("stage_results", []))
    final_answer = llm_service.summarize_run(request, stage_results)
    return {
        "current_stage": "finalize",
        "status": "completed",
        "final_answer": final_answer,
    }


def build_workflow() -> Any:
    """Compile the dev-team state graph.

    Flow:
        START -> planner -> developer -> reviewer
        reviewer --changes_requested--> developer   (bounded loop)
        tester --failed--> developer                 (one bounded repair pass)
        developer --missing change--> implementation_halt -> END
        reviewer --passed--> tester -> docs -> finalize -> END
    """
    graph = StateGraph(DevTeamState)

    for agent_name in STAGE_ORDER:
        graph.add_node(agent_name, _agent_node(agent_name))
    graph.add_node("finalize", finalize_node)
    graph.add_node("implementation_halt", implementation_halt_node)
    graph.add_node("review_halt", review_halt_node)
    graph.add_node("validation_halt", validation_halt_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "developer")
    graph.add_conditional_edges(
        "developer",
        route_after_developer,
        {"reviewer": "reviewer", "implementation_halt": "implementation_halt"},
    )
    graph.add_edge("implementation_halt", END)
    graph.add_conditional_edges(
        "reviewer",
        route_after_review,
        {
            "developer": "developer",
            "tester": "tester",
            "review_halt": "review_halt",
        },
    )
    graph.add_edge("review_halt", END)
    graph.add_conditional_edges(
        "tester",
        route_after_tester,
        {
            "developer": "developer",
            "docs": "docs",
            "validation_halt": "validation_halt",
        },
    )
    graph.add_edge("validation_halt", END)
    graph.add_edge("docs", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()


@lru_cache(maxsize=1)
def get_workflow() -> Any:
    return build_workflow()
