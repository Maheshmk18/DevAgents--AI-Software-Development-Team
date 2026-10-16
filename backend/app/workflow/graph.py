from __future__ import annotations

from functools import lru_cache
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from app.core.agent_registry import create_agent
from app.services.llm_service import llm_service
from app.workflow.orchestrator import STAGE_ORDER
from app.workflow.state import DevTeamState

#: How many times the reviewer may send work back to the developer before the
#: run is allowed to continue to validation. Keeps the loop bounded.
MAX_REVIEW_ATTEMPTS = 2


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
        return "tester"
    return "developer"


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
        reviewer --passed--> tester -> docs -> finalize -> END
    """
    graph = StateGraph(DevTeamState)

    for agent_name in STAGE_ORDER:
        graph.add_node(agent_name, _agent_node(agent_name))
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "developer")
    graph.add_edge("developer", "reviewer")
    graph.add_conditional_edges(
        "reviewer",
        route_after_review,
        {"developer": "developer", "tester": "tester"},
    )
    graph.add_edge("tester", "docs")
    graph.add_edge("docs", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()


@lru_cache(maxsize=1)
def get_workflow() -> Any:
    return build_workflow()
