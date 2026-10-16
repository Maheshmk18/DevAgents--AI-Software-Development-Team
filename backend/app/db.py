import time
from datetime import datetime, timezone

from pymongo import MongoClient
from pymongo.database import Database

from app.config import settings


_MEMORY_FALLBACK = {
    "runs": {},
    "approvals": {},
    "evaluations": {},
}

#: Server-selection timeout. Kept short so an unreachable Mongo fails fast
#: instead of stalling the request that triggered it.
_SERVER_SELECTION_TIMEOUT_MS = 2000

#: How long a Mongo availability result is trusted before it is probed again.
#: Without this cache, *every* operation would block for the full server-selection
#: timeout while Mongo is down, which makes the whole API feel broken.
_PROBE_TTL_SECONDS = 30.0

client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=_SERVER_SELECTION_TIMEOUT_MS)
database: Database = client[settings.mongo_db]

#: Cached decision: ``True`` means "use the in-memory fallback".
_memory_mode: bool | None = None
_last_probe_at: float = 0.0


def _remember(memory_mode: bool) -> bool:
    global _memory_mode, _last_probe_at
    _memory_mode = memory_mode
    _last_probe_at = time.monotonic()
    return memory_mode


def _normalize_value(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    return value


def ping_database() -> dict[str, str]:
    try:
        client.admin.command("ping")
        _remember(False)
        return {"status": "ok", "database": settings.mongo_db}
    except Exception as exc:
        _remember(True)
        return {"status": "error", "database": settings.mongo_db, "detail": str(exc)}


def ensure_indexes() -> None:
    if _use_memory():
        return
    try:
        database.runs.create_index("created_at")
        database.runs.create_index("status")
        database.approvals.create_index("status")
        database.evaluations.create_index("run_id")
    except Exception:
        return


def _use_memory() -> bool:
    """Whether database access should use the in-memory fallback.

    The answer is cached for ``_PROBE_TTL_SECONDS`` so an unreachable Mongo does
    not cost every single operation a server-selection timeout.
    """
    if _memory_mode is None or (time.monotonic() - _last_probe_at) >= _PROBE_TTL_SECONDS:
        try:
            client.admin.command("ping")
            return _remember(False)
        except Exception:
            return _remember(True)
    return _memory_mode


def insert_run(document: dict) -> dict:
    normalized = _normalize_value(document)
    if _use_memory():
        _MEMORY_FALLBACK["runs"][normalized["id"]] = normalized
        return normalized
    # Upsert: runs are re-persisted on every status change, so a plain insert
    # would raise DuplicateKeyError from the second write onwards.
    database.runs.replace_one({"id": normalized["id"]}, normalized, upsert=True)
    return normalized


def list_runs() -> list[dict]:
    if _use_memory():
        return list(_MEMORY_FALLBACK["runs"].values())
    return list(database.runs.find().sort("created_at", -1))


def get_run(run_id: str) -> dict | None:
    if _use_memory():
        return _MEMORY_FALLBACK["runs"].get(run_id)
    return database.runs.find_one({"id": run_id})


def list_approvals() -> list[dict]:
    if _use_memory():
        return list(_MEMORY_FALLBACK["approvals"].values())
    return list(database.approvals.find().sort("created_at", -1))


def get_approval(approval_id: str) -> dict | None:
    if _use_memory():
        return _MEMORY_FALLBACK["approvals"].get(approval_id)
    return database.approvals.find_one({"id": approval_id})


def insert_approval(document: dict) -> dict:
    normalized = _normalize_value(document)
    if _use_memory():
        _MEMORY_FALLBACK["approvals"][normalized["id"]] = normalized
        return normalized
    database.approvals.replace_one({"id": normalized["id"]}, normalized, upsert=True)
    return normalized


def update_approval(approval_id: str, update: dict) -> dict | None:
    if _use_memory():
        existing = _MEMORY_FALLBACK["approvals"].get(approval_id)
        if existing is None:
            return None
        existing.update(update)
        _MEMORY_FALLBACK["approvals"][approval_id] = existing
        return existing
    result = database.approvals.find_one_and_update(
        {"id": approval_id},
        {"$set": _normalize_value(update)},
        return_document=True,
    )
    return result


def list_evaluations() -> list[dict]:
    if _use_memory():
        return list(_MEMORY_FALLBACK["evaluations"].values())
    return list(database.evaluations.find().sort("created_at", -1))


def get_evaluation(run_id: str) -> dict | None:
    if _use_memory():
        return _MEMORY_FALLBACK["evaluations"].get(run_id)
    return database.evaluations.find_one({"run_id": run_id})


def insert_evaluation(document: dict) -> dict:
    normalized = _normalize_value(document)
    if _use_memory():
        _MEMORY_FALLBACK["evaluations"][normalized["run_id"]] = normalized
        return normalized
    database.evaluations.replace_one({"run_id": normalized["run_id"]}, normalized, upsert=True)
    return normalized

