from __future__ import annotations

import json
import re
import time
from typing import Any

from app.config import settings

#: Fenced-code fence used by some models when returning JSON.
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

#: After a provider call fails, skip the provider for this many seconds so one
#: broken key does not cost every agent in the run a slow retry.
FAILURE_COOLDOWN_SECONDS = 15.0

#: Bound provider stalls so one agent call cannot hold up the workflow for
#: minutes before the user sees a useful failure or fallback.
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_SDK_RETRIES = 0

#: Give a rate-limited call one retry and honor the provider's reset guidance,
#: up to one minute, instead of failing after an arbitrary short wait.
MAX_RATE_LIMIT_RETRIES = 1
MAX_RATE_LIMIT_WAIT_SECONDS = 60.0

#: Groq states the wait in its error message, e.g. "Please try again in 15.6s".
_RETRY_AFTER = re.compile(r"try again in ([\d.]+)\s*s", re.IGNORECASE)
_DURATION_PART = re.compile(r"([\d.]+)\s*([hms])", re.IGNORECASE)


def _parse_duration_seconds(value: Any) -> float | None:
    """Parse Groq reset values such as ``7.66s`` or ``2m59.56s``."""
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip().lower()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass

    parts = list(_DURATION_PART.finditer(text))
    if not parts:
        return None
    normalized = "".join(part.group(0).replace(" ", "") for part in parts)
    if normalized != text.replace(" ", ""):
        return None
    units = {"h": 3600.0, "m": 60.0, "s": 1.0}
    return sum(float(part.group(1)) * units[part.group(2)] for part in parts)


def is_rate_limit_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a provider rate limit (HTTP 429)."""
    if getattr(exc, "status_code", None) == 429:
        return True
    if "RateLimit" in type(exc).__name__:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text


def rate_limit_delay(exc: BaseException) -> float:
    """How long to wait before retrying a rate-limited call."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        # Groq's reset-requests header is the daily request window (RPD), while
        # reset-tokens is the TPM window that causes these generation failures.
        for key in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
            try:
                value = headers.get(key)
            except Exception:  # pragma: no cover - defensive
                value = None
            if value:
                seconds = _parse_duration_seconds(value)
                if seconds is not None:
                    # Leave a small margin so the retry does not race the reset.
                    return max(1.0, seconds + 2.0)
    match = _RETRY_AFTER.search(str(exc))
    if match:
        try:
            return max(1.0, float(match.group(1)) + 2.0)
        except ValueError:  # pragma: no cover - defensive
            pass
    return 60.0


class LLMService:
    """LangChain-backed LLM access for the dev-team agents.

    Groq is the one and only provider (`langchain_groq.ChatGroq`). It is exposed
    through a LangChain chat model so the agents can bind tools and run real
    tool-calling loops.

    With no usable key — or when the provider call fails — the service reports a
    deterministic ``fallback`` so the workflow still completes offline. The last
    call outcome is kept on the instance so :meth:`status` can explain *why*.
    """

    def __init__(self) -> None:
        self.last_provider: str | None = None
        self.last_error: str | None = None
        self._unavailable_until: float = 0.0

    # -- provider selection -------------------------------------------------
    def provider_chain(self) -> list[str]:
        """Configured providers in priority order (Groq only)."""
        return ["groq"] if settings.groq_api_key else []

    @staticmethod
    def model_for(provider: str) -> str:
        return settings.groq_model

    # -- model construction -------------------------------------------------
    def _build_model(self, provider: str, temperature: float) -> Any:
        from langchain_groq import ChatGroq

        return ChatGroq(
            model=settings.groq_model,
            api_key=settings.groq_api_key,
            temperature=temperature,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=MAX_SDK_RETRIES,
        )

    def get_chat_model(
        self,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        tool_choice: str | dict[str, Any] | bool | None = None,
    ) -> Any | None:
        """Return a tool-bound LangChain chat model, or ``None`` if unavailable.

        ``None`` is the signal for callers to use their deterministic fallback.
        A recent failure short-circuits the whole chain for a short cooldown so a
        broken provider does not add latency to every agent in a run.
        """
        if time.monotonic() < self._unavailable_until:
            return None

        for provider in self.provider_chain():
            try:
                model = self._build_model(provider, temperature)
                if tools:
                    model = model.bind_tools(tools, tool_choice=tool_choice)
            except Exception as exc:  # pragma: no cover - depends on credentials
                self.last_error = f"{provider}: {exc.__class__.__name__}: {str(exc)[:240]}"
                continue
            self.last_provider = provider
            self.last_error = None
            return model
        return None

    # -- invocation with rate-limit recovery --------------------------------
    def invoke(self, model: Any, messages: list[Any]) -> Any:
        """Invoke ``model`` with one bounded retry for provider rate limits.

        Honor the provider's retry guidance up to one minute; other errors are
        raised unchanged.
        """
        attempt = 0
        while True:
            try:
                return model.invoke(messages)
            except Exception as exc:
                attempt += 1
                if attempt > MAX_RATE_LIMIT_RETRIES or not is_rate_limit_error(exc):
                    raise
                time.sleep(min(rate_limit_delay(exc), MAX_RATE_LIMIT_WAIT_SECONDS))

    # -- text generation ----------------------------------------------------
    def generate(self, prompt: str, system_prompt: str | None = None) -> dict[str, Any]:
        model = self.get_chat_model()
        if model is None:
            if self.last_error is None:
                self.last_error = "No Groq API key configured. Set GROQ_API_KEY in backend/.env."
            self.last_provider = "fallback"
            return {"provider": "fallback", "text": ""}

        from langchain_core.messages import HumanMessage, SystemMessage

        messages: list[Any] = []
        if system_prompt:
            messages.append(SystemMessage(system_prompt))
        messages.append(HumanMessage(prompt))

        try:
            response = self.invoke(model, messages)
            text = response.content if isinstance(response.content, str) else str(response.content)
            text = text.strip()
            if not text:
                raise ValueError("empty response from provider")
            self.last_provider = self.last_provider or "llm"
            self.last_error = None
            return {"provider": self.last_provider, "text": text}
        except Exception as exc:  # pragma: no cover - depends on credentials
            self.mark_failure(exc)
            return {"provider": "fallback", "text": ""}

    def generate_json(self, prompt: str, system_prompt: str) -> dict[str, Any] | None:
        """Ask the model for JSON and parse it defensively.

        Returns ``None`` whenever the LLM is unavailable or the payload cannot be
        parsed, so callers can fall back to a deterministic structure.
        """
        result = self.generate(prompt, system_prompt)
        if result.get("provider") == "fallback":
            return None
        text = result.get("text", "")
        fence = _JSON_FENCE.search(text)
        if fence:
            text = fence.group(1)
        text = text.strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    # -- diagnostics --------------------------------------------------------
    def probe(self) -> dict[str, Any]:
        """Make one cheap call to learn whether the provider actually works.

        Run at startup in a background thread: it populates ``status()`` for the
        console badge and, if the provider is broken, starts the failure cooldown
        before the first user request — so that request is not slow.
        """
        if not self.provider_chain():
            return self.status()
        self.generate("Reply with the single word: ok")
        return self.status()

    def mark_failure(self, exc: BaseException) -> None:
        """Record a provider failure so ``status()`` can explain the fallback."""
        failed_provider = self.last_provider or "llm"
        self.last_provider = "fallback"
        if is_rate_limit_error(exc):
            retry_delay = rate_limit_delay(exc)
            limit_kind = "tokens per minute" if re.search(
                r"tokens per minute|\bTPM\b", str(exc), re.IGNORECASE
            ) else "requests"
            self.last_error = (
                f"{failed_provider}: HTTP 429 rate limit ({limit_kind}); "
                f"provider retry window is about {retry_delay:.0f}s."
            )
            # Let the next workflow run try again; it will honor the provider's
            # retry guidance instead of being hidden behind the generic cooldown.
            self._unavailable_until = 0.0
            return
        self.last_error = f"{failed_provider}: {exc.__class__.__name__}: {str(exc)[:240]}"
        self._unavailable_until = time.monotonic() + FAILURE_COOLDOWN_SECONDS

    def status(self) -> dict[str, Any]:
        configured = bool(self.provider_chain())
        return {
            "configured": configured,
            "primary_provider": "groq" if configured else "none",
            "model": settings.groq_model if configured else None,
            "last_provider": self.last_provider,
            "last_error": self.last_error,
        }

    # -- agent-facing helpers ----------------------------------------------
    def summarize_run(self, request: str, stage_results: list[dict[str, Any]]) -> str:
        prompt = (
            f"Original request: {request}\n\n"
            "Stage results:\n"
            + "\n".join(
                f"- {item.get('agent', 'unknown')}: {item.get('summary', 'completed')}"
                for item in stage_results
            )
            + "\n\nWrite a concise final response that explains what was completed and any follow-ups."
        )
        response = self.generate(
            prompt, "You are a senior engineering coordinator summarizing a completed workflow."
        )
        if response.get("provider") == "fallback":
            lines = [
                f"- {item.get('agent', 'unknown')}: {item.get('summary', 'completed')}"
                for item in stage_results
            ]
            return "The workflow has completed. Stage summary:\n" + "\n".join(lines)
        return response.get("text") or "The workflow has completed successfully."


llm_service = LLMService()
