"""Backend tool-calling loop shared by every dev-team agent.

Each role receives only its permitted tool schemas. Workspace writes and
allowlisted validation run during the workflow; GitHub and Gmail actions wait
for the final human approval.
"""

from __future__ import annotations

import json
from contextvars import ContextVar, Token
from typing import Any, Callable
from uuid import uuid4

from app.core.util import truncate
from app.mcp.tool_registry import MCPToolRegistry, function_name_to_tool
from app.services.llm_service import llm_service

#: Hard cap on tool-calling round trips so a confused model cannot loop forever.
MAX_TOOL_STEPS = 6

# Cap tool fanout across the full agent run as well as the number of model
# round trips. Some providers return dozens of calls in one response.
MAX_TOOL_CALLS_BY_AGENT = {
    # Leave room for the root view, focused directory listings, and searches.
    "planner": 6,
    # A useful implementation may need several focused reads and separate writes.
    "developer": 8,
    "reviewer": 4,
    "tester": 1,
    "docs": 4,
}

#: How much of a tool result is fed back into the model conversation.
MAX_TOOL_RESULT_CHARS = 8000

_TOOL_EVENT_SINK: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "agent_tool_event_sink", default=None
)


def set_tool_event_sink(sink: Callable[[dict[str, Any]], None] | None) -> Token:
    """Route tool lifecycle events to the active backend workflow, if any."""
    return _TOOL_EVENT_SINK.set(sink)


def reset_tool_event_sink(token: Token) -> None:
    _TOOL_EVENT_SINK.reset(token)


def publish_tool_event(event: dict[str, Any]) -> None:
    sink = _TOOL_EVENT_SINK.get()
    if sink is not None:
        try:
            sink(dict(event))
        except Exception:
            return


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
        force_initial_tool_call: bool = False,
        tool_allowlist: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Run the loop, or return ``None`` when no LLM is configured.

        The caller treats ``None`` as "use the deterministic fallback".
        """
        role_tools = set(self.registry.get_tools_for_agent(self.agent_name))
        allowed = role_tools if tool_allowlist is None else role_tools.intersection(tool_allowlist)
        schemas = [
            schema
            for schema in self.registry.get_tool_schemas_for_agent(self.agent_name)
            if function_name_to_tool(str(schema.get("function", {}).get("name", ""))) in allowed
        ]
        model = llm_service.get_chat_model(
            tools=schemas or None,
            tool_choice="required" if force_initial_tool_call and schemas else None,
        )
        if model is None:
            return None

        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

        messages: list[Any] = [SystemMessage(system_prompt), HumanMessage(user_prompt)]

        events: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        summary = ""
        steps = 0
        tool_calls_used = 0
        tool_call_limit = MAX_TOOL_CALLS_BY_AGENT.get(self.agent_name, 4)

        for steps in range(1, max_steps + 1):
            try:
                response = llm_service.invoke(model, messages)
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
                if tool_calls_used >= tool_call_limit:
                    event = {
                        "event_id": uuid4().hex,
                        "agent": self.agent_name,
                        "tool": tool_name,
                        "status": "blocked",
                        "arguments": arguments,
                        "error": f"The {self.agent_name} tool-call limit ({tool_call_limit}) was reached.",
                    }
                    events.append(event)
                    publish_tool_event(event)
                    messages.append(
                        ToolMessage(content=event["error"], tool_call_id=call_id)
                    )
                    continue
                tool_calls_used += 1

                if tool_name not in allowed:
                    events.append(
                        {
                            "agent": self.agent_name,
                            "tool": tool_name,
                            "status": "blocked",
                            "arguments": arguments,
                            "error": f"Tool is not available to the {self.agent_name} agent.",
                        }
                    )
                    messages.append(
                        ToolMessage(
                            content=f"'{tool_name}' is not permitted for the {self.agent_name} agent.",
                            tool_call_id=call_id,
                        )
                    )
                    continue

                event_id = uuid4().hex
                if self.registry.requires_human_approval(tool_name):
                    deferred.append(
                        {
                            "event_id": event_id,
                            "agent": self.agent_name,
                            "tool": tool_name,
                            "arguments": arguments,
                        }
                    )
                    event = {
                        "event_id": event_id,
                        "agent": self.agent_name,
                        "tool": tool_name,
                        "status": "awaiting_approval",
                        "arguments": arguments,
                    }
                    events.append(event)
                    publish_tool_event(event)
                    messages.append(
                        ToolMessage(
                            content=(
                                f"'{tool_name}' reaches an external service and was queued for "
                                "the final human approval. It was NOT executed."
                            ),
                            tool_call_id=call_id,
                        )
                    )
                    continue

                if not self.registry.can_run_automatically(tool_name, arguments):
                    event = {
                        "event_id": event_id,
                        "agent": self.agent_name,
                        "tool": tool_name,
                        "status": "blocked",
                        "arguments": arguments,
                        "error": "This tool call is outside the workspace or validation allowlist.",
                    }
                    events.append(event)
                    publish_tool_event(event)
                    messages.append(
                        ToolMessage(content=event["error"], tool_call_id=call_id)
                    )
                    continue

                publish_tool_event(
                    {
                        "event_id": event_id,
                        "agent": self.agent_name,
                        "tool": tool_name,
                        "status": "running",
                        "arguments": arguments,
                    }
                )

                try:
                    meta = self.registry.call_tool_with_meta(tool_name, arguments)
                    result = meta.get("result")
                    command_failed = (
                        tool_name == "repo.run_command"
                        and isinstance(result, dict)
                        and result.get("return_code") != 0
                    )
                    event: dict[str, Any] = {
                        "event_id": event_id,
                        "agent": self.agent_name,
                        "tool": tool_name,
                        "status": "error" if command_failed else (
                            "executed" if tool_name in {"repo.write_file", "repo.run_command"} else "ok"
                        ),
                        "arguments": arguments,
                        "via": meta.get("via"),
                        "result": truncate(result),
                    }
                    if command_failed:
                        event["error"] = str(
                            result.get("stderr") or result.get("stdout") or "Validation command failed."
                        )[:1200]
                    if meta.get("mcp_error"):
                        event["mcp_error"] = meta["mcp_error"]
                    events.append(event)
                    publish_tool_event(event)
                    messages.append(
                        ToolMessage(content=self._render(result), tool_call_id=call_id)
                    )
                except Exception as exc:  # a tool failure must not break the run
                    event = {
                        "event_id": event_id,
                        "agent": self.agent_name,
                        "tool": tool_name,
                        "status": "error",
                        "arguments": arguments,
                        "error": str(exc),
                    }
                    events.append(event)
                    publish_tool_event(event)
                    messages.append(
                        ToolMessage(content=f"Tool error: {exc}", tool_call_id=call_id)
                    )

            if force_initial_tool_call and steps == 1:
                # Force one actual tool action, then restore normal tool choice
                # so the model can finish with a summary instead of being
                # forced to keep calling tools for every round trip.
                model = llm_service.get_chat_model(tools=schemas or None)
                if model is None:
                    break

        return {"summary": summary, "events": events, "deferred": deferred, "steps": steps}

    @staticmethod
    def _render(result: Any) -> str:
        try:
            text = json.dumps(result, default=str)
        except (TypeError, ValueError):
            text = str(result)
        if len(text) > MAX_TOOL_RESULT_CHARS:
            omitted = len(text) - MAX_TOOL_RESULT_CHARS
            text = f"{text[:MAX_TOOL_RESULT_CHARS]}\n...[tool result truncated; {omitted} characters omitted]"
        return text or "(empty result)"
