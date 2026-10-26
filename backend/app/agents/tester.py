from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent, last_stage_result
from app.workflow.state import DevTeamState


def _normalized_path(value: object) -> str:
    return str(value or "").replace("\\", "/").casefold().lstrip("./")


def _is_test_path(value: object) -> bool:
    path = _normalized_path(value)
    parts = path.split("/")
    filename = parts[-1] if parts else ""
    return (
        any(part in {"test", "tests"} for part in parts[:-1])
        or filename.startswith("test_")
        or filename.endswith("_test.py")
    )


def _paths_match(changed_path: object, reported_path: object) -> bool:
    changed = _normalized_path(changed_path)
    reported = _normalized_path(reported_path)
    return bool(changed and reported and (reported.endswith("/" + changed) or reported == changed))


SYSTEM_PROMPT = (
    "You are the TESTER on a five-agent software engineering team "
    "(planner, developer, reviewer, tester, docs).\n"
    "Your job is to validate the reviewed implementation.\n\n"
    "How to work:\n"
    "- Run exactly this backend validation command once: "
    "python -m pytest backend/tests -q.\n"
    "- Do not run any other commands. Do not use shell syntax, GitHub, or Gmail.\n"
    "- Finish with a short summary of the command result."
)


class TestingAgent(BaseAgent):
    """Runs supported validation commands against the reviewed workspace."""

    name = "tester"

    def run(self, state: DevTeamState) -> dict[str, Any]:
        review = last_stage_result(state, "reviewer") or {}
        checks = [
            "unit tests",
            "integration checks",
            "build validation",
            "smoke validation",
        ]

        changed_files = [
            str(event.get("arguments", {}).get("file_path"))
            for event in state.get("tool_events", [])
            if event.get("tool") == "repo.write_file"
            and event.get("status") == "executed"
            and event.get("arguments", {}).get("file_path")
        ]
        entry, events = self.work(
            status="validated",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=(
                f"Feature request:\n{state.get('request', '')}\n\n"
                f"Reviewer summary:\n{review.get('summary', '(no review output)')}\n\n"
                f"Applied files: {', '.join(dict.fromkeys(changed_files)) or '(none)'}\n\n"
                "Run exactly: python -m pytest backend/tests -q."
            ),
            fallback_calls=[("repo.run_command", {"command": "python -m pytest backend/tests -q"})],
            fallback_summary="Validation suite assembled and executed against the implementation.",
            details={"checks": checks},
        )
        command_events = [event for event in events if event.get("tool") == "repo.run_command"]
        successful_commands = [event for event in command_events if event.get("status") == "executed"]
        failed_commands = [event for event in command_events if event.get("status") in {"error", "blocked"}]
        if failed_commands:
            diagnostic = "\n".join(
                str(event.get("error") or "")
                for event in failed_commands
            )
            command_results = [
                event.get("result")
                for event in failed_commands
                if isinstance(event.get("result"), dict)
            ]
            collection_failed = any(
                result.get("collection_failed")
                for result in command_results
            ) or "ERROR collecting" in diagnostic or "errors during collection" in diagnostic
            collection_error_paths = [
                str(path)
                for result in command_results
                for path in result.get("collection_error_paths", [])
            ]
            failed_test_paths = {
                _normalized_path(test_id)
                for event in failed_commands
                for test_id in (event.get("result") or {}).get("failed_test_ids", [])
            }
            changed_test_paths = {_normalized_path(path) for path in changed_files if _is_test_path(path)}
            matching_failures = {
                failed_path
                for failed_path in failed_test_paths
                if any(
                    failed_path.split("::", 1)[0].endswith(test_path.removeprefix("backend/"))
                    or test_path.endswith(failed_path.split("::", 1)[0].removeprefix("backend/"))
                    for test_path in changed_test_paths
                )
            }
            changed_source_paths = [path for path in changed_files if not _is_test_path(path)]
            matching_collection_source = any(
                _paths_match(changed_path, source_path)
                for changed_path in changed_source_paths
                for source_path in collection_error_paths
            )
            repairable = matching_collection_source if collection_failed else (
                not changed_test_paths or not failed_test_paths or bool(matching_failures)
            )
            entry["status"] = "validation_failed"
            entry.setdefault("details", {})["repairable"] = repairable
            entry["details"]["collection_failed"] = collection_failed
            entry["details"]["collection_error_paths"] = collection_error_paths
            entry.setdefault("details", {})["failed_test_paths"] = sorted(failed_test_paths)
            if collection_failed and matching_collection_source:
                entry["summary"] = (
                    "Pytest could not collect the backend tests because a changed source file has a syntax or import error. "
                    "The developer will repair that file and validation will retry."
                )
            elif collection_failed:
                entry["summary"] = (
                    "Pytest could not collect the backend tests because of a syntax or import error outside the changed source files. "
                    "Review the collection diagnostic before retrying."
                )
            elif changed_test_paths and failed_test_paths and not matching_failures:
                entry["summary"] = (
                    "The changed test did not fail, but the full backend suite has failures in other tests. "
                    "Review the reported baseline failures; the developer will not rewrite this feature."
                )
            else:
                entry["summary"] = (
                    "One or more validation commands failed or were blocked. "
                    "See the preserved failure summary in tool activity."
                )
        elif successful_commands:
            entry["status"] = "validated"
            entry["summary"] = "Validation command(s) completed successfully in the backend."
        else:
            entry["status"] = "validation_missing"
            entry["summary"] = "The tester did not run a supported validation command."
        return {"current_stage": self.name, "tool_events": events, "stage_results": [entry]}
