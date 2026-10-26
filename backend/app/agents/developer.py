from __future__ import annotations

import json
import re
from typing import Any

from app.agents.base import BaseAgent, extract_path_tokens, last_stage_result
from app.agents.runtime import publish_tool_event
from app.services.llm_service import llm_service
from app.workflow.state import DevTeamState

SYSTEM_PROMPT = (
    "You are the DEVELOPER on a five-agent software engineering team "
    "(planner, developer, reviewer, tester, docs).\n"
    "Your job is to turn the plan into a concrete, reviewable implementation.\n\n"
    "How to work:\n"
    "- Inspect the real workspace first; never invent file contents.\n"
    "- For FastAPI endpoints, inspect the existing router wiring. If `backend/app/api/routes.py` "
    "already defines the `/api` router and `backend/app/main.py` includes it, add the endpoint "
    "to that router instead of creating another router or changing `main.py`. If a separate "
    "router is necessary, register it with one complete `app.include_router(router)` call.\n"
    "- Before adding a route, check whether an equivalent path and method already exist. "
    "Update the existing handler instead of appending a duplicate endpoint.\n"
    "- Keep inspection focused: read the relevant files in one batch, then edit.\n"
    "- When validation reports a Python syntax or import error, inspect the exact changed file "
    "and line from the traceback, then make the smallest correction. Preserve unrelated code "
    "and route descriptions; do not rewrite an entire file for a one-line repair.\n"
    "- Write complete changes with repo.write_file in the workspace. These changes "
    "are applied by the backend so the reviewer and tester can inspect them.\n"
    "- You own every requested code edit, including creating or updating test files. "
    "The tester only runs existing checks and cannot write files.\n"
    "- For a purely additive change to an existing file, use repo.write_file with "
    "append=true and provide only the new snippet. Preserve all unrelated content.\n"
    "- Do not set repo.write_file allow_shrink=true unless the user explicitly asked "
    "to remove or replace most of an existing file.\n"
    "- You must call repo.write_file for every requested code or documentation "
    "change before you summarize. Do not return a plan or claim completion without a write.\n"
    "- Do not run tests or compile commands; the tester and reviewer handle validation.\n"
    "- Do not edit outside the workspace or modify environment secret files.\n"
    "- Finish with a short summary: what you changed, in which files, and why it is safe."
)


class DeveloperAgent(BaseAgent):
    """Applies the implementation inside the workspace."""

    name = "developer"

    def run(self, state: DevTeamState) -> dict[str, Any]:
        request = state.get("request", "")
        plan = state.get("plan", [])
        attempt = state.get("review_attempts", 0) + 1

        target_files = extract_path_tokens(request)
        review_note = ""
        review = last_stage_result(state, "reviewer")
        if review and review.get("status") == "changes_requested":
            review_note = (
                "\nA previous attempt was sent back by the reviewer. "
                f"Address this feedback:\n{review.get('summary', '')}\n"
            )
        validation_note = self._validation_feedback(state)

        plan_lines = "\n".join(
            f"- {task.get('title', '')}" for task in plan if isinstance(task, dict)
        )
        test_change_required = self._requires_test_change(request, plan)
        implementation_change_required = self._requires_implementation_change(request)
        user_prompt = (
            f"Feature request:\n{request}\n\n"
            f"Planned tasks:\n{plan_lines or '(none)'}\n"
            f"Files named in the request: {', '.join(target_files) or '(none)'}\n"
            f"{review_note}\n"
            f"{validation_note}\n"
            "Implement every requested change now, including requested test files. "
            "Read only the relevant files. For additive changes to an existing file, "
            "call repo.write_file with append=true and only the new snippet; for edits, "
            "preserve and write the complete updated content."
        )

        fallback_calls: list[tuple[str, dict[str, Any]]] = [
            ("repo.read_file", {"file_path": path}) for path in target_files
        ]
        entry, events = self.work(
            status="implementation_ready",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            fallback_calls=fallback_calls,
            fallback_summary=f"Prepared an implementation for {len(plan)} planned task(s) (attempt {attempt}).",
            force_initial_tool_call=True,
            details={
                "attempt": attempt,
                "tasks_planned": len(plan),
                "target_files": target_files,
                "test_change_required": test_change_required,
                "implementation_change_required": implementation_change_required,
            },
        )
        writes = self._successful_writes(events)
        recovery_attempts = 0
        missing_test_write = test_change_required and not self._has_test_write(writes)
        missing_implementation_write = (
            implementation_change_required and not self._has_implementation_write(writes)
        )
        write_errors = self._failed_write_events(events)
        initial_engine = entry.get("details", {}).get("engine")
        provider_failed_before_recovery = bool(llm_service.status().get("last_error"))
        # A test request needs a separate, test-focused follow-up when the first
        # implementation pass (or its first recovery) still omitted the test.
        max_recovery_attempts = 2 if test_change_required else 1
        while (
            initial_engine == "llm"
            and not provider_failed_before_recovery
            and (not writes or missing_test_write or missing_implementation_write or write_errors)
            and recovery_attempts < max_recovery_attempts
        ):
            recovery_attempts += 1
            event_notes = self._format_tool_events(events)
            shrink_rejections = [
                event for event in write_errors
                if "much shorter than the existing file" in str(event.get("error", "")).casefold()
            ]
            if shrink_rejections:
                rejected_paths = list(dict.fromkeys(
                    str(event.get("arguments", {}).get("file_path") or "the rejected file")
                    for event in shrink_rejections
                ))
                retry_test = (
                    f" Also write the requested test at `{self._test_file_hint(request, plan, events)}`."
                    if missing_test_write
                    else ""
                )
                recovery_instruction = (
                    "The write guard rejected a shortened replacement for "
                    f"{', '.join(rejected_paths)} to protect existing code. Keep the guard enabled. "
                    "For this additive change, retry with repo.write_file append=true and content "
                    "containing only the requested new snippet. Do not resend a shortened full-file "
                    f"replacement or set allow_shrink=true.{retry_test}"
                )
            elif missing_test_write and not missing_implementation_write:
                test_file_hint = self._test_file_hint(request, plan, events)
                recovery_instruction = (
                    "The feature implementation was written, but the requested test file was not. "
                    f"Write the test at `{test_file_hint}`. Assert the requested behavior, "
                    "including the successful response status and returned JSON when the request "
                    "specifies them. Do not rewrite the implementation."
                )
            elif missing_test_write:
                test_file_hint = self._test_file_hint(request, plan, events)
                recovery_instruction = (
                    "No implementation was written. Implement the feature and its requested "
                    f"test file at `{test_file_hint}` now."
                )
            elif missing_implementation_write:
                recovery_instruction = (
                    "The requested feature implementation has not been written yet. Apply the "
                    "feature change in its source file now, preserving all unrelated content."
                )
            else:
                recovery_instruction = (
                    "No implementation was written. Use the file contents from the previous "
                    "tool activity and apply the complete implementation now."
                )
            if write_errors and not shrink_rejections:
                failed_paths = list(dict.fromkeys(
                    str(event.get("arguments", {}).get("file_path") or "unknown file")
                    for event in write_errors
                ))
                recovery_instruction = (
                    f"Retry the failed write(s) for {', '.join(failed_paths)} using the tool error "
                    "details below. Do not repeat writes that already succeeded. "
                    + (
                        f"Also create the requested test at `{self._test_file_hint(request, plan, events)}`. "
                        if missing_test_write else ""
                    )
                )
            recovery_prompt = (
                f"Feature request:\n{request}\n\n"
                f"Planned tasks:\n{plan_lines or '(none)'}\n"
                f"Files named in the request: {', '.join(target_files) or '(none)'}\n"
                f"{review_note}\n"
                f"{validation_note}\n"
                "RECOVERY REQUIRED: "
                f"{recovery_instruction} This is recovery attempt {recovery_attempts} "
                f"of {max_recovery_attempts}. Do not repeat inspection or summarize the plan. "
                "Call repo.write_file with the content and append setting specified above. This recovery only "
                "has the write tool available.\n"
                f"Previous response: {entry.get('summary', '(empty)')}\n"
                f"Previous tool activity:\n{event_notes or '(no tool calls)'}"
            )
            recovery_entry, recovery_events = self.work(
                status="implementation_ready",
                system_prompt=SYSTEM_PROMPT,
                user_prompt=recovery_prompt,
                fallback_calls=fallback_calls,
                fallback_summary=f"Retried implementation for {len(plan)} planned task(s).",
                force_initial_tool_call=True,
                tool_allowlist=["repo.write_file"],
                details={
                    "attempt": attempt,
                    "tasks_planned": len(plan),
                    "target_files": target_files,
                    "test_change_required": test_change_required,
                    "implementation_change_required": implementation_change_required,
                    "recovery_attempt": recovery_attempts,
                },
            )
            events.extend(recovery_events)
            # Keep the retry's final model response and record both bounded
            # model calls in the stage details.
            recovery_details = dict(entry.get("details", {}))
            first_steps = recovery_details.get("llm_steps", 0)
            recovery_details.update(recovery_entry.get("details", {}))
            recovery_details["recovery_attempted"] = True
            recovery_details["recovery_attempts"] = recovery_attempts
            recovery_details["recovery_engine"] = recovery_entry.get("details", {}).get("engine")
            recovery_details["engine"] = initial_engine
            recovery_details["llm_steps"] = first_steps + recovery_entry.get("details", {}).get("llm_steps", 0)
            recovery_entry["details"] = recovery_details
            entry = recovery_entry
            writes = self._successful_writes(events)
            self._mark_recovered_write_errors(events, writes)
            write_errors = self._failed_write_events(events)
            missing_test_write = test_change_required and not self._has_test_write(writes)
            missing_implementation_write = (
                implementation_change_required and not self._has_implementation_write(writes)
            )

        if missing_test_write and not missing_implementation_write and initial_engine == "llm":
            contract_test = self._contract_api_test(request, plan, events)
            if contract_test:
                test_path, test_content, test_file_exists = contract_test
                fallback_events, _ = self.invoke_tools([(
                    "repo.write_file",
                    {
                        "file_path": test_path,
                        "content": test_content,
                        **({"append": True} if test_file_exists else {}),
                    },
                )])
                events.extend(fallback_events)
                writes = self._successful_writes(events)
                self._mark_recovered_write_errors(events, writes)
                write_errors = self._failed_write_events(events)
                missing_test_write = not self._has_test_write(writes)
                entry.setdefault("details", {})["contract_test_fallback"] = {
                    "file_path": test_path,
                    "written": not missing_test_write,
                }

        self._mark_recovered_write_errors(events, writes)
        write_errors = self._failed_write_events(events)
        model_status = llm_service.status()
        if entry.get("details", {}).get("engine") == "deterministic" or (
            model_status.get("last_error") and not writes
        ):
            entry["status"] = "implementation_unavailable"
            entry.setdefault("details", {})["model_status"] = model_status
            if not model_status.get("configured"):
                reason = "No LLM provider is configured. Set GROQ_API_KEY in backend/.env and restart the backend."
            elif model_status.get("last_error"):
                last_error = str(model_status.get("last_error", ""))
                if "http 429 rate limit" in last_error.casefold():
                    reason = (
                        "The Groq token-per-minute limit remained reached after the automatic wait. "
                        f"Wait for the provider window, then retry this request; no repository reset is needed. ({last_error})"
                    )
                else:
                    reason = "The configured model request failed. Check GET /api/llm for its status and retry when it is healthy."
            else:
                reason = "The configured model is unavailable. Check GET /api/llm for its status and retry."
            entry["summary"] = f"The developer could not get an AI response, so no files were written. {reason}"
        elif write_errors:
            entry["status"] = "implementation_failed"
            entry["summary"] = "The developer could not apply one or more workspace changes. See tool activity for details."
        elif missing_implementation_write:
            entry["status"] = "implementation_missing"
            entry["summary"] = "The developer did not write the requested feature implementation."
        elif missing_test_write:
            entry["status"] = "implementation_missing"
            entry["summary"] = (
                "The developer implemented the feature but did not write the requested test "
                f"after {recovery_attempts} recovery attempts."
            )
        elif not writes:
            entry["status"] = "implementation_missing"
            if recovery_attempts == 1:
                entry["summary"] = (
                    "The developer still did not apply a file change after one recovery attempt. "
                    "Review the tool activity and retry if needed."
                )
            elif recovery_attempts > 1:
                entry["summary"] = (
                    f"The developer still did not apply a file change after {recovery_attempts} recovery attempts. "
                    "Review the tool activity and retry if needed."
                )
            else:
                entry["summary"] = (
                    "The developer did not apply a file change. Review the tool activity and retry if needed."
                )
        return {"current_stage": self.name, "tool_events": events, "stage_results": [entry]}

    @staticmethod
    def _validation_feedback(state: DevTeamState) -> str:
        """Return the latest failed test output so a repair pass has useful context."""
        tester = last_stage_result(state, "tester")
        if not tester or tester.get("status") == "validated":
            return ""

        failures = [
            event for event in state.get("tool_events", [])
            if event.get("agent") == "tester"
            and event.get("tool") == "repo.run_command"
            and event.get("status") in {"error", "blocked"}
        ]
        detail = ""
        if failures:
            latest = failures[-1]
            detail = str(latest.get("error") or latest.get("result") or "")[:3000]
        detail = detail or str(tester.get("summary") or "The validation run failed.")
        return (
            "\nThe tester ran the checks and they failed. Fix the implementation that caused "
            "the failure, then let review and validation run again. Do not only edit the test. "
            f"Failure output:\n{detail}\n"
        )

    @staticmethod
    def _successful_writes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            event for event in events
            if event.get("tool") == "repo.write_file" and event.get("status") == "executed"
        ]

    @staticmethod
    def _failed_write_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            event for event in events
            if event.get("tool") == "repo.write_file"
            and event.get("status") in {"error", "blocked"}
        ]

    @staticmethod
    def _requires_test_change(request: str, plan: list[dict[str, Any]]) -> bool:
        test_change = re.compile(
            r"\b(?:add|write|create|implement|update|modify|edit|include)\b.{0,100}\btests?\b"
            r"|\btests?\b.{0,100}\b(?:add|write|create|implement|update|modify|edit|include)\b",
            re.IGNORECASE,
        )
        if test_change.search(request or ""):
            return True
        return any(
            isinstance(task, dict) and test_change.search(str(task.get("title") or ""))
            for task in plan
        )

    @staticmethod
    def _requires_implementation_change(request: str) -> bool:
        feature_change = re.compile(
            r"\b(?:add|create|implement|update|modify)\b.{0,80}"
            r"\b(?:get|post|put|patch|delete)\s+/",
            re.IGNORECASE,
        )
        return any(
            "test" not in sentence.casefold() and feature_change.search(sentence)
            for sentence in re.split(r"[.!?\n]+", request or "")
        )

    @staticmethod
    def _has_implementation_write(writes: list[dict[str, Any]]) -> bool:
        return any(
            not DeveloperAgent._is_test_path(
                str(event.get("arguments", {}).get("file_path") or "")
            )
            for event in writes
        )

    @staticmethod
    def _has_test_write(writes: list[dict[str, Any]]) -> bool:
        for event in writes:
            path = str(event.get("arguments", {}).get("file_path") or "")
            if DeveloperAgent._is_test_path(path):
                return True
        return False

    @staticmethod
    def _is_test_path(path: str) -> bool:
        normalized = path.replace("\\", "/").casefold()
        parts = normalized.split("/")
        name = parts[-1] if parts else ""
        return (
            any(part in {"test", "tests", "spec", "specs"} for part in parts[:-1])
            or name.startswith("test_")
            or re.search(r"_test\.[^.]+$", name) is not None
            or ".test." in name
            or ".spec." in name
        )

    @staticmethod
    def _test_file_hint(
        request: str,
        plan: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> str:
        """Suggest a concrete test path from the plan and repository layout."""
        plan_text = " ".join(
            str(task.get("title", "")) for task in plan if isinstance(task, dict)
        )
        for source in (request, plan_text):
            for path in extract_path_tokens(source):
                normalized = path.replace("\\", "/")
                if DeveloperAgent._has_test_write([{"arguments": {"file_path": normalized}}]):
                    return normalized

        listed_files: list[str] = []
        for event in events:
            result = event.get("result")
            if event.get("tool") == "repo.list_files":
                files = result.get("files", []) if isinstance(result, dict) else result
                if isinstance(files, list):
                    listed_files.extend(str(path).replace("\\", "/") for path in files)
        test_dirs = []
        for path in listed_files:
            parts = path.split("/")[:-1]
            for index, part in enumerate(parts):
                if part.casefold() in {"test", "tests", "spec", "specs"}:
                    test_dirs.append("/".join(parts[: index + 1]))
                    break

        endpoint = re.search(r"\bGET\s+(/[\w/{}/.-]+)", request, re.IGNORECASE)
        if endpoint:
            route_parts = [part for part in endpoint.group(1).strip("/").split("/") if part]
            route_name = next((part for part in reversed(route_parts) if not part.startswith("{")), "endpoint")
            route_name = re.sub(r"[^a-zA-Z0-9]+", "_", route_name).strip("_").lower() or "endpoint"
            test_dir = next((path for path in test_dirs if "backend" in path.casefold()), None)
            test_dir = test_dir or ("backend/tests" if "/api/" in endpoint.group(1).casefold() else "tests")
            return f"{test_dir}/test_{route_name}_endpoint.py"

        test_dir = next((path for path in test_dirs if "backend" in path.casefold()), None)
        test_dir = test_dir or (test_dirs[0] if test_dirs else "tests")
        return f"{test_dir}/test_requested_change.py"

    @staticmethod
    def _contract_api_test(
        request: str,
        plan: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> tuple[str, str, bool] | None:
        """Build a smoke test for an explicit FastAPI GET/JSON contract."""
        endpoint = re.search(r"\bGET\s+(/[\w/{}/.-]+)", request, re.IGNORECASE)
        if not endpoint:
            return None
        route = endpoint.group(1).rstrip(".,)")
        payload: dict[str, Any] | None = None
        for candidate in reversed(re.findall(r"\{[^{}]*\}", request, re.DOTALL)):
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                payload = parsed
                break
        if payload is None:
            return None

        listed_files: list[str] = []
        for event in events:
            result = event.get("result")
            if event.get("tool") == "repo.list_files":
                files = result.get("files", []) if isinstance(result, dict) else result
                if isinstance(files, list):
                    listed_files.extend(str(path).replace("\\", "/") for path in files)
        normalized_files = {path.casefold() for path in listed_files}
        has_fastapi_app = any(
            path.endswith("/app/main.py") or path == "app/main.py"
            for path in normalized_files
        ) or any(
            event.get("tool") == "repo.read_file"
            and str(event.get("arguments", {}).get("file_path", "")).replace("\\", "/").casefold().endswith("/app/main.py")
            for event in events
        )
        if not has_fastapi_app:
            return None

        test_path = DeveloperAgent._test_file_hint(request, plan, events)
        route_parts = [part for part in route.strip("/").split("/") if part]
        route_name = next((part for part in reversed(route_parts) if not part.startswith("{")), "endpoint")
        route_name = re.sub(r"[^a-zA-Z0-9]+", "_", route_name).strip("_").lower() or "endpoint"
        expected_json = json.dumps(payload, ensure_ascii=False)
        test_content = (
            "from fastapi.testclient import TestClient\n"
            "from app.main import app\n\n"
            "client = TestClient(app)\n\n\n"
            f"def test_{route_name}_endpoint_returns_expected_json() -> None:\n"
            f"    response = client.get({route!r})\n"
            "    assert response.status_code == 200\n"
            f"    assert response.json() == {expected_json}\n"
        )
        return test_path, test_content, test_path.casefold() in normalized_files

    @staticmethod
    def _mark_recovered_write_errors(
        events: list[dict[str, Any]],
        writes: list[dict[str, Any]],
    ) -> None:
        successful_paths = {
            str(event.get("arguments", {}).get("file_path") or "")
            for event in writes
        }
        if not successful_paths:
            return
        for event in events:
            if (
                event.get("tool") == "repo.write_file"
                and event.get("status") in {"error", "blocked"}
                and str(event.get("arguments", {}).get("file_path") or "") in successful_paths
            ):
                event["status"] = "recovered"
                event["resolution"] = "A later write to the same file succeeded."
                publish_tool_event(event)

    @staticmethod
    def _format_tool_events(events: list[dict[str, Any]]) -> str:
        lines = []
        for event in events:
            arguments = event.get("arguments", {})
            path = arguments.get("file_path")
            description = f"{event.get('tool')} [{event.get('status')}]"
            if path:
                description += f" {path}"
            detail = event.get("error") or event.get("result")
            if detail:
                description += f": {detail}"
            lines.append(f"- {description}")
        return "\n".join(lines)[:4000]

    @staticmethod
    def _implementation_notes(request: str, plan: list[dict[str, Any]], attempt: int) -> str:
        lines = [
            "# Implementation Notes",
            "",
            f"Request: {request.strip()}",
            f"Attempt: {attempt}",
            "",
            "## Planned tasks",
        ]
        lines.extend(f"- {task.get('id', '?')}: {task.get('title', '')}" for task in plan)
        lines.extend(
            [
                "",
                "## Notes",
                "- Confirm the change is scoped to the interfaces touched above.",
                "- Run the validation suite before opening a pull request.",
                "",
            ]
        )
        return "\n".join(lines)
