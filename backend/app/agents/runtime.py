"""The LLM-driven tool-calling loop shared by every dev-team agent.

This is the piece that makes the agents *real* subagents rather than template
fillers: the model is handed its role's tool schemas, decides which tools to
call, and the runtime feeds the results back until the model stops calling
tools and produces a final answer.

Safety is enforced here, not by trust:

* only tools mapped to the agent's role are exposed (and re-checked here);
* read-only tools are executed automatically;
* side-effecting tools are **never** executed — they are collected as
  ``deferred`` proposals for a human to approve via the approval gate.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.util import truncate
from app.mcp.tool_registry import MCPToolRegistry, function_name_to_tool
from app.services.llm_service import llm_service

#: Hard cap on tool-calling round trips so a confused model cannot loop forever.
MAX_TOOL_STEPS = 6

#: How much of a tool result is fed back into the model conversation.
MAX_TOOL_RESULT_CHARS = 1600


class AgentRuntime:
    """Runs one role-scoped, LLM-driven tool-calling loop."""

    def __init__(self, agent_name: str, registry: MCPToolRegistry | None = None) -> None:
        self.agent_name = agent_name
        self.registry = registry or MCPToolRegistry()

    def run(
        self,
        system_prompt: str,
        user_prompt: str,
        max_steps: int = MAX_TOOL_STEPS,
    ) -> dict[str, Any] | None:
        """Run the loop, or return ``None`` when no LLM is configured.

        The caller treats ``None`` as "use the deterministic fallback".
        """
        schemas = self.registry.get_tool_schemas_for_agent(self.agent_name)
        model = llm_service.get_chat_model(tools=schemas or None)
        if model is None:
            return None

        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

        allowed = set(self.registry.get_tools_for_agent(self.agent_name))
        messages: list[Any] = [SystemMessage(system_prompt), HumanMessage(user_prompt)]

        events: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        summary = ""
        steps = 0

        for steps in range(1, max_steps + 1):
            try:
                response = model.invoke(messages)
            except Exception as exc:  # provider/network failure
                llm_service.mark_failure(exc)
                if not events and not deferred:
                    # Nothing happened yet: let the caller use the deterministic path.
                    return None
                break

            messages.append(response)

            content = response.content if isinstance(response.content, str) else ""
            if content.strip():
                summary = content.strip()

            calls = list(getattr(response, "tool_calls", None) or [])
            if not calls:
                break

            for index, call in enumerate(calls):
                tool_name = function_name_to_tool(str(call.get("name", "")))
                arguments = call.get("args") or {}
                call_id = str(call.get("id") or f"{self.agent_name}-{steps}-{index}")

                if tool_name not in allowed:
                    messages.append(
                        ToolMessage(
                            content=f"'{tool_name}' is not permitted for the {self.agent_name} agent.",
                            tool_call_id=call_id,
                        )
                    )
                    continue

                if not self.registry.is_read_only(tool_name):
                    deferred.append(
                        {"agent": self.agent_name, "tool": tool_name, "arguments": arguments}
                    )
                    messages.append(
                        ToolMessage(
                            content=(
                                f"'{tool_name}' is side-effecting and was queued for human "
                                "approval. It was NOT executed; do not expect a result."
                            ),
                            tool_call_id=call_id,
                        )
                    )
                    continue

                try:
                    meta = self.registry.call_tool_with_meta(tool_name, arguments)
                    event: dict[str, Any] = {
                        "agent": self.agent_name,
                        "tool": tool_name,
                        "status": "ok",
                        "via": meta.get("via"),
                        "result": truncate(meta.get("result")),
                    }
                    if meta.get("mcp_error"):
                        event["mcp_error"] = meta["mcp_error"]
                    events.append(event)
                    messages.append(
                        ToolMessage(content=self._render(meta.get("result")), tool_call_id=call_id)
                    )
                except Exception as exc:  # a tool failure must not break the run
                    events.append(
                        {"agent": self.agent_name, "tool": tool_name, "status": "error", "error": str(exc)}
                    )
                    messages.append(
                        ToolMessage(content=f"Tool error: {exc}", tool_call_id=call_id)
                    )

        return {"summary": summary, "events": events, "deferred": deferred, "steps": steps}

    @staticmethod
    def _render(result: Any) -> str:
        try:
            text = json.dumps(result, default=str)
        except (TypeError, ValueError):
            text = str(result)
        return text[:MAX_TOOL_RESULT_CHARS] or "(empty result)"
