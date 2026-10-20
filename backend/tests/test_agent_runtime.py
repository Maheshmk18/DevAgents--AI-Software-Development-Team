"""Tests for the LLM-driven, tool-calling agent runtime.

The runtime is the core of the "real subagents" behaviour, so it is tested with
a scripted chat model instead of a live provider: that keeps the test
deterministic while still exercising the full tool-calling loop, the role tool
policy, and the side-effect safety gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage

from app.agents.runtime import AgentRuntime
from app.agents.developer import DeveloperAgent
from app.mcp.tool_registry import MCPToolRegistry, function_name_to_tool, tool_to_function_name
from app.mcp.repository import RepositoryMCPClient
from app.services.agent_run_context import agent_run_context
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


def test_unscoped_repo_write_is_blocked(tmp_path, monkeypatch) -> None:
    registry = MCPToolRegistry(use_mcp=False)
    registry.clients["repo"] = RepositoryMCPClient(root_path=str(tmp_path))
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

    # The read-only call executes, but a write outside a run branch is refused.
    assert [event["tool"] for event in turn["events"]] == ["repo.list_files", "repo.write_file"]
    assert turn["events"][0]["status"] == "ok"
    assert turn["events"][1]["status"] == "blocked"
    assert turn["deferred"] == []

    # It must not exist on disk.
    assert not (tmp_path / "nope.txt").exists()


def test_repo_write_runs_inside_the_approved_isolated_branch(tmp_path, monkeypatch) -> None:
    registry = MCPToolRegistry(use_mcp=False)
    model = ScriptedModel(
        [
            _tool_call("repo__write_file", {"file_path": "src/api.py", "content": "ready = True\n"}, "t1"),
            AIMessage(content="Implemented in the isolated branch."),
        ]
    )
    monkeypatch.setattr(llm_service, "get_chat_model", lambda **_: model)

    with agent_run_context(tmp_path, "acme/widget"):
        turn = AgentRuntime("developer", registry).run("system", "Implement the API")

    assert turn is not None
    assert turn["events"][0]["status"] == "executed"
    assert (tmp_path / "src" / "api.py").read_text(encoding="utf-8") == "ready = True\n"


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
    assert len(turn["events"]) == 1
    assert turn["events"][0]["status"] == "blocked"
    assert turn["deferred"] == []


def test_tool_call_fanout_is_bounded(monkeypatch) -> None:
    registry = MCPToolRegistry(use_mcp=False)
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "repo__run_command",
                        "args": {"command": "python -m pytest backend/tests -q"},
                        "id": f"call-{index}",
                        "type": "tool_call",
                    }
                    for index in range(5)
                ],
            ),
            AIMessage(content="Finished."),
        ]
    )
    monkeypatch.setattr(llm_service, "get_chat_model", lambda **_: model)

    turn = AgentRuntime("tester", registry).run("system", "user")

    assert turn is not None
    assert len(turn["events"]) == 5
    assert turn["events"][0]["status"] == "blocked"  # unscoped runs cannot execute
    assert all("tool-call limit (1)" in event.get("error", "") for event in turn["events"][1:])


def test_planner_call_limit_matches_its_repository_inspection_budget(monkeypatch) -> None:
    registry = MCPToolRegistry(use_mcp=False)
    monkeypatch.setattr(registry, "_call_direct", lambda tool_name, arguments: [])
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "repo__list_files",
                        "args": {"relative_path": ".", "max_files": 1},
                        "id": f"plan-{index}",
                        "type": "tool_call",
                    }
                    for index in range(4)
                ],
            ),
            AIMessage(content="Plan ready."),
        ]
    )
    monkeypatch.setattr(llm_service, "get_chat_model", lambda **_: model)

    turn = AgentRuntime("planner", registry).run("system", "user")

    assert turn is not None
    assert [event["status"] for event in turn["events"]] == ["ok", "ok", "ok", "blocked"]
    assert "planner tool-call limit (3)" in turn["events"][-1]["error"]


def test_required_initial_tool_call_switches_back_to_auto(monkeypatch) -> None:
    registry = MCPToolRegistry(use_mcp=False)
    monkeypatch.setattr(registry, "_call_direct", lambda tool_name, arguments: [])
    model = ScriptedModel(
        [
            _tool_call("repo__list_files", {"relative_path": ".", "max_files": 1}, "required-read"),
            AIMessage(content="Finished inspecting."),
        ]
    )
    model_options: list[dict[str, Any]] = []

    def get_model(**options):
        model_options.append(options)
        return model

    monkeypatch.setattr(llm_service, "get_chat_model", get_model)

    turn = AgentRuntime("developer", registry).run(
        "system", "user", force_initial_tool_call=True
    )

    assert turn is not None
    assert model_options[0]["tool_choice"] == "required"
    assert model_options[1].get("tool_choice") is None
    assert turn["summary"] == "Finished inspecting."


def test_llm_service_passes_required_tool_choice_to_provider(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    class BindableModel:
        def bind_tools(self, tools, **kwargs):
            observed["tools"] = tools
            observed.update(kwargs)
            return self

    monkeypatch.setattr(llm_service, "provider_chain", lambda: ["groq"])
    monkeypatch.setattr(llm_service, "_build_model", lambda provider, temperature: BindableModel())

    llm_service.get_chat_model(tools=[{"type": "function"}], tool_choice="required")

    assert observed["tool_choice"] == "required"


def test_backend_pytest_command_uses_backend_working_directory(tmp_path, monkeypatch) -> None:
    backend = tmp_path / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "tests").mkdir()
    observed: dict[str, Any] = {}

    def fake_run(args, **kwargs):
        observed["args"] = args
        observed.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="passed", stderr="")

    monkeypatch.setattr("app.mcp.repository.subprocess.run", fake_run)
    client = RepositoryMCPClient(root_path=str(tmp_path))

    result = client.run_command("python -m pytest backend/tests -q")

    assert result["return_code"] == 0
    assert observed["cwd"] == str(backend)
    assert observed["args"] == [sys.executable, "-m", "pytest", "-q"]
    assert str(backend) in observed["env"]["PYTHONPATH"].split(os.pathsep)


def test_runtime_returns_none_without_an_llm(monkeypatch) -> None:
    registry = MCPToolRegistry(use_mcp=False)
    monkeypatch.setattr(llm_service, "get_chat_model", lambda **_: None)

    assert AgentRuntime("planner", registry).run("system", "user") is None


def test_developer_retries_once_when_model_does_not_write(monkeypatch) -> None:
    agent = DeveloperAgent()
    work_calls: list[dict[str, Any]] = []

    def scripted_work(**kwargs):
        work_calls.append(kwargs)
        if len(work_calls) == 1:
            return (
                {
                    "agent": "developer",
                    "status": "implementation_ready",
                    "summary": "I inspected the routes and made a plan.",
                    "details": {"engine": "llm", "llm_steps": 2},
                },
                [{"tool": "repo.read_file", "status": "ok", "arguments": {"file_path": "backend/app/main.py"}}],
            )
        return (
            {
                "agent": "developer",
                "status": "implementation_ready",
                "summary": "Added the ping route and test.",
                "details": {"engine": "llm", "llm_steps": 3},
            },
            [{
                "tool": "repo.write_file",
                "status": "executed",
                "arguments": {"file_path": "backend/app/api/routes.py"},
            }],
        )

    monkeypatch.setattr(agent, "work", scripted_work)

    result = agent.run({
        "request": "Add GET /api/ping returning {\"message\":\"pong\"}.",
        "plan": [{"title": "Implement GET /api/ping"}],
        "review_attempts": 0,
    })

    entry = result["stage_results"][0]
    assert len(work_calls) == 2
    assert "RECOVERY REQUIRED" in work_calls[1]["user_prompt"]
    assert "repo.read_file [ok]" in work_calls[1]["user_prompt"]
    assert entry["status"] == "implementation_ready"
    assert entry["details"]["recovery_attempted"] is True
    assert entry["details"]["llm_steps"] == 5
    assert [event["tool"] for event in result["tool_events"]] == [
        "repo.read_file",
        "repo.write_file",
    ]


def test_developer_recovery_applies_write_in_isolated_workspace(tmp_path, monkeypatch) -> None:
    agent = DeveloperAgent()
    no_write_model = ScriptedModel([AIMessage(content="I inspected the request and made a plan.")])
    recovery_model = ScriptedModel(
        [
            _tool_call(
                "repo__write_file",
                {"file_path": "backend/app/api/ping.py", "content": 'MESSAGE = "pong"\n'},
                "recovery-write",
            ),
            AIMessage(content="Applied the ping implementation."),
        ]
    )
    models = iter([no_write_model, recovery_model, recovery_model])
    monkeypatch.setattr(llm_service, "get_chat_model", lambda **_: next(models))

    with agent_run_context(tmp_path, "acme/widget"):
        result = agent.run({
            "request": "Add GET /api/ping returning {\"message\":\"pong\"}.",
            "plan": [{"title": "Implement GET /api/ping"}],
            "review_attempts": 0,
        })

    assert result["stage_results"][0]["status"] == "implementation_ready"
    assert result["stage_results"][0]["details"]["recovery_attempted"] is True
    assert (tmp_path / "backend" / "app" / "api" / "ping.py").read_text(encoding="utf-8") == 'MESSAGE = "pong"\n'


def test_developer_does_not_retry_without_llm(monkeypatch) -> None:
    agent = DeveloperAgent()
    work_calls = 0

    monkeypatch.setattr(
        llm_service,
        "status",
        lambda: {"configured": False, "primary_provider": "none", "last_error": None},
    )

    def scripted_work(**kwargs):
        nonlocal work_calls
        work_calls += 1
        return (
            {
                "agent": "developer",
                "status": "implementation_ready",
                "summary": "No AI model configured.",
                "details": {"engine": "deterministic"},
            },
            [],
        )

    monkeypatch.setattr(agent, "work", scripted_work)

    result = agent.run({"request": "Add a route", "plan": [], "review_attempts": 0})

    assert work_calls == 1
    assert result["stage_results"][0]["status"] == "implementation_unavailable"
    assert result["stage_results"][0]["details"]["model_status"]["configured"] is False
    assert "GROQ_API_KEY" in result["stage_results"][0]["summary"]


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
            raise RuntimeError("insufficient_quota")

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
        "model": "qwen/qwen3.8-27b",
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
            model="qwen/qwen3.8-27b",
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


# --- rate-limit recovery ---------------------------------------------------
# A free Groq tier enforces an input-token budget per minute, so a 429 is
# *transient*: it must be waited out and retried, never turned into a fallback.


def test_rate_limit_is_retried_instead_of_falling_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import llm_service as module

    registry = MCPToolRegistry(use_mcp=False)

    class FlakyModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, messages: list[Any]) -> AIMessage:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("429 rate limit reached; please try again in 5s")
            return AIMessage(content="Recovered after the rate limit window.")

    model = FlakyModel()
    monkeypatch.setattr(llm_service, "get_chat_model", lambda **_: model)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    turn = AgentRuntime("planner", registry).run("system", "user")

    assert turn is not None, "a transient rate limit must not force the fallback"
    assert model.calls == 2
    assert turn["summary"] == "Recovered after the rate limit window."


def test_rate_limit_detection_and_delay_hint() -> None:
    from app.services.llm_service import is_rate_limit_error, rate_limit_delay

    assert is_rate_limit_error(RuntimeError("429 Too Many Requests")) is True
    assert is_rate_limit_error(RuntimeError("connection reset by peer")) is False
    # The provider usually says how long to wait; +2s of safety margin is added.
    assert rate_limit_delay(RuntimeError("please try again in 12.5s")) == 14.5


def test_rate_limit_delay_uses_groq_token_reset_header() -> None:
    from types import SimpleNamespace

    from app.services.llm_service import rate_limit_delay

    error = RuntimeError("429 token rate limit")
    error.response = SimpleNamespace(headers={
        "x-ratelimit-reset-tokens": "34.0s",
        # This is the request-per-day window, not the token-per-minute window.
        "x-ratelimit-reset-requests": "2m59.56s",
    })

    assert rate_limit_delay(error) == 36.0


def test_rate_limit_delay_parses_duration_header() -> None:
    from types import SimpleNamespace

    from app.services.llm_service import rate_limit_delay

    error = RuntimeError("429 request rate limit")
    error.response = SimpleNamespace(headers={"x-ratelimit-reset-requests": "2m59.56s"})

    assert rate_limit_delay(error) == 181.56


def test_rate_limit_does_not_bench_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient 429 must not disable the provider for the next agent in the run."""
    monkeypatch.setattr(llm_service, "provider_chain", lambda: ["groq"])
    monkeypatch.setattr(llm_service, "_build_model", lambda provider, temperature: object())

    llm_service.mark_failure(RuntimeError("429 rate limit reached"))

    assert llm_service.last_provider == "fallback"
    assert llm_service.get_chat_model() is not None, "provider should stay available after a 429"
