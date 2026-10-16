from app.agents.base import BaseAgent
from app.agents.developer import DeveloperAgent
from app.agents.docs import DocumentationAgent
from app.agents.planner import PlannerAgent
from app.agents.reviewer import ReviewAgent
from app.agents.tester import TestingAgent


AGENT_REGISTRY: dict[str, type[BaseAgent]] = {
    "planner": PlannerAgent,
    "developer": DeveloperAgent,
    "reviewer": ReviewAgent,
    "tester": TestingAgent,
    "docs": DocumentationAgent,
}


def create_agent(agent_name: str) -> BaseAgent:
    agent_cls = AGENT_REGISTRY.get(agent_name)
    if agent_cls is None:
        raise ValueError(f"Unknown agent: {agent_name}")
    return agent_cls()
