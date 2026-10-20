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
        # MongoDB stores and returns UTC, but hands back *naive* datetimes. Tag
        # them as UTC instead of letting ``astimezone`` assume local time, which
        # would shift every timestamp by the server's UTC offset.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    return value


def _clean(document: dict | None) -> dict | None:
    """Prepare a document for the API layer.

    Drops Mongo's internal ``_id`` (a BSON ``ObjectId`` that JSON cannot
    represent) and makes every timestamp timezone-aware UTC.
    """
    if document is None:
        return None
    cleaned = dict(document)
    cleaned.pop("_id", None)
    return _normalize_value(cleaned)


def ping_database() -> dict[str, str]:
    try:
        client.admin.command("ping")
        _remember(False)
        return {"status": "ok", "database": settings.mongo_db}
    except Exception as exc:
        _remember(True)
        return {"status": "error", "database": settings.mongo_db, "detail": str(exc)}


def database_status() -> dict[str, str]:
    """Cheap availability view for the UI.

    Reuses the cached probe (``_PROBE_TTL_SECONDS``) instead of pinging on every
    request, so the console can show the *real* state without paying a
    server-selection timeout each time it refreshes.
    """
    if _use_memory():
        return {"status": "error", "database": settings.mongo_db}
    return {"status": "ok", "database": settings.mongo_db}


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
    normalized = _clean(document)
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
    return [_clean(doc) for doc in database.runs.find().sort("created_at", -1)]


def get_run(run_id: str) -> dict | None:
    if _use_memory():
        return _MEMORY_FALLBACK["runs"].get(run_id)
    return _clean(database.runs.find_one({"id": run_id}))


def list_approvals() -> list[dict]:
    if _use_memory():
        return list(_MEMORY_FALLBACK["approvals"].values())
    return [_clean(doc) for doc in database.approvals.find().sort("created_at", -1)]


def get_approval(approval_id: str) -> dict | None:
    if _use_memory():
        return _MEMORY_FALLBACK["approvals"].get(approval_id)
    return _clean(database.approvals.find_one({"id": approval_id}))


def insert_approval(document: dict) -> dict:
    normalized = _clean(document)
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
        {"$set": _normalize_value({key: value for key, value in update.items() if key != "_id"})},
        return_document=True,
    )
    return _clean(result)


def list_evaluations() -> list[dict]:
    if _use_memory():
        return list(_MEMORY_FALLBACK["evaluations"].values())
    return [_clean(doc) for doc in database.evaluations.find().sort("created_at", -1)]


def get_evaluation(run_id: str) -> dict | None:
    if _use_memory():
        return _MEMORY_FALLBACK["evaluations"].get(run_id)
    return _clean(database.evaluations.find_one({"run_id": run_id}))


def delete_run(run_id: str) -> None:
    """Delete a run and everything that hangs off it (approvals, evaluation).

    The console lists one card per approval and one entry per evaluation, so
    leaving orphans behind is what made old test runs keep cluttering the UI.
    """
    if _use_memory():
        _MEMORY_FALLBACK["runs"].pop(run_id, None)
        for approval_id in [
            key
            for key, value in _MEMORY_FALLBACK["approvals"].items()
            if value.get("run_id") == run_id
        ]:
            _MEMORY_FALLBACK["approvals"].pop(approval_id, None)
        _MEMORY_FALLBACK["evaluations"].pop(run_id, None)
        return
    database.runs.delete_one({"id": run_id})
    database.approvals.delete_many({"run_id": run_id})
    database.evaluations.delete_many({"run_id": run_id})


def delete_all_runs() -> int:
    """Wipe the whole run history. Returns how many runs were removed."""
    if _use_memory():
        count = len(_MEMORY_FALLBACK["runs"])
        _MEMORY_FALLBACK["runs"].clear()
        _MEMORY_FALLBACK["approvals"].clear()
        _MEMORY_FALLBACK["evaluations"].clear()
        return count
    count = database.runs.count_documents({})
    database.runs.delete_many({})
    database.approvals.delete_many({})
    database.evaluations.delete_many({})
    return count


def insert_evaluation(document: dict) -> dict:
    normalized = _clean(document)
    if _use_memory():
        _MEMORY_FALLBACK["evaluations"][normalized["run_id"]] = normalized
        return normalized
    database.evaluations.replace_one({"run_id": normalized["run_id"]}, normalized, upsert=True)
    return normalized
