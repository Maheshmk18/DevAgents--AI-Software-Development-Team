"""Tests for the LLM-driven, tool-calling agent runtime.

The runtime is the core of the "real subagents" behaviour, so it is tested with
a scripted chat model instead of a live provider: that keeps the test
deterministic while still exercising the full tool-calling loop, the role tool
policy, and the side-effect safety gate.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage

from app.agents.runtime import AgentRuntime
from app.mcp.tool_registry import MCPToolRegistry, function_name_to_tool, tool_to_function_name
from app.services.llm_service import llm_service


class ScriptedModel:
    """A stand-in LangChain chat model that replays a fixed list of replies."""

    def __init__(self, replies: list[AIMessage]) -> None:
        self._replies = list(replies)
        self.conversations: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.conversations.append(list(messages))
        if not self._replies:
            return AIMessage(content="done")
        return self._replies.pop(0)


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def test_tool_name_mangling_round_trips() -> None:
    assert tool_to_function_name("repo.read_file") == "repo__read_file"
    assert function_name_to_tool("repo__read_file") == "repo.read_file"


def test_readonly_tool_runs_and_side_effecting_tool_is_deferred(monkeypatch) -> None:
    registry = MCPToolRegistry(use_mcp=False)
    model = ScriptedModel(
        [
            _tool_call("repo__list_files", {"relative_path": ".", "max_files": 5}, "t1"),
            _tool_call("repo__write_file", {"file_path": "nope.txt", "content": "x"}, "t2"),
            AIMessage(content="Plan ready. VERDICT: APPROVED"),
        ]
    )
    monkeypatch.setattr(llm_service, "get_chat_model", lambda **_: model)

    turn = AgentRuntime("developer", registry).run("system", "user")

    assert turn is not None
    assert turn["summary"] == "Plan ready. VERDICT: APPROVED"
    assert turn["steps"] == 3

    # The read-only call executed...
    assert [event["tool"] for event in turn["events"]] == ["repo.list_files"]
    assert turn["events"][0]["status"] == "ok"

    # ...while the side-effecting write was only proposed, never executed.
    assert [action["tool"] for action in turn["deferred"]] == ["repo.write_file"]
    assert all(registry.is_side_effecting(a["tool"]) for a in turn["deferred"])

    # It must not exist on disk.
    assert not (registry.clients["repo"].root_path / "nope.txt").exists()


def test_tool_outside_the_role_is_refused(monkeypatch) -> None:
    registry = MCPToolRegistry(use_mcp=False)
    model = ScriptedModel(
        [
            # The tester may not send email — that tool is not exposed to it.
            _tool_call("gmail__send_email", {"to": "a@b.c", "subject": "s", "body": "b"}, "t1"),
            AIMessage(content="Nothing else to do."),
        ]
    )
    monkeypatch.setattr(llm_service, "get_chat_model", lambda **_: model)

    turn = AgentRuntime("tester", registry).run("system", "user")

    assert turn is not None
    assert turn["events"] == []
    assert turn["deferred"] == []


def test_runtime_returns_none_without_an_llm(monkeypatch) -> None:
    registry = MCPToolRegistry(use_mcp=False)
    monkeypatch.setattr(llm_service, "get_chat_model", lambda **_: None)

    assert AgentRuntime("planner", registry).run("system", "user") is None


def test_runtime_falls_back_when_the_provider_errors(monkeypatch) -> None:
    registry = MCPToolRegistry(use_mcp=False)

    class ExplodingModel:
        def invoke(self, messages: list[Any]) -> AIMessage:
            raise RuntimeError("provider exploded")

    monkeypatch.setattr(llm_service, "get_chat_model", lambda **_: ExplodingModel())

    assert AgentRuntime("planner", registry).run("system", "user") is None
    assert "provider exploded" in (llm_service.last_error or "")


def test_failed_provider_is_short_circuited(monkeypatch) -> None:
    """A recorded failure makes get_chat_model() return None during the cooldown."""
    from app.services import llm_service as module

    monkeypatch.setattr(llm_service, "provider_chain", lambda: ["groq"])

    class ExplodingModel:
        def invoke(self, messages: list[Any]) -> AIMessage:
            raise RuntimeError("429 insufficient_quota")

    monkeypatch.setattr(llm_service, "_build_model", lambda provider, temperature: ExplodingModel())

    assert llm_service.get_chat_model() is not None  # before any failure

    llm_service.generate("hi")
    assert llm_service.last_provider == "fallback"
    assert module.FAILURE_COOLDOWN_SECONDS > 0

    # Now the chain is short-circuited without even building a model.
    assert llm_service.get_chat_model() is None


# --- offline end-to-end: real LangChain model over a mocked HTTP transport ---


def _chat_completion(message: dict[str, Any], finish_reason: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "llama-3.3-70b-versatile",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def test_langchain_tool_calling_path_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drives a real ``ChatGroq`` through the whole loop with no network.

    This is the strongest offline proof that the LLM path works: the model
    requests a tool, the runtime executes it and feeds the result back, and the
    model then answers — all over a mocked HTTP transport.
    """
    from langchain_groq import ChatGroq

    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=_chat_completion(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "repo__list_files",
                                    "arguments": json.dumps({"relative_path": ".", "max_files": 3}),
                                },
                            }
                        ],
                    },
                    "tool_calls",
                ),
            )
        return httpx.Response(
            200,
            json=_chat_completion(
                {"role": "assistant", "content": "Repository inspected; plan is ready."}, "stop"
            ),
        )

    monkeypatch.setattr(llm_service, "provider_chain", lambda: ["groq"])
    monkeypatch.setattr(
        llm_service,
        "_build_model",
        lambda provider, temperature: ChatGroq(
            model="llama-3.3-70b-versatile",
            api_key="test-key",
            temperature=temperature,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        ),
    )

    registry = MCPToolRegistry(use_mcp=False)
    turn = AgentRuntime("planner", registry).run("system", "user")

    assert turn is not None
    assert turn["summary"] == "Repository inspected; plan is ready."
    assert [event["tool"] for event in turn["events"]] == ["repo.list_files"]

    # The model received the role's function schemas on the first turn...
    assert requests[0]["tools"], "tool schemas were not sent to the model"
    assert requests[0]["tools"][0]["function"]["name"].startswith("repo__")
    # ...and the tool result was fed back on the second turn.
    second_turn = requests[1]["messages"]
    assert any(item.get("role") == "tool" for item in second_turn)
