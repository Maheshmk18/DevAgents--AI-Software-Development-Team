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
        database.runs.create_index("owner_id")
        database.approvals.create_index("status")
        database.approvals.create_index("owner_id")
        database.evaluations.create_index("run_id")
        database.evaluations.create_index("owner_id")
        database.user_credentials.create_index(
            [("user_id", 1), ("provider", 1)], unique=True
        )
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


def _require_database() -> None:
    """Credential storage must be durable; never silently put tokens in memory."""
    try:
        client.admin.command("ping")
        _remember(False)
    except Exception as exc:
        _remember(True)
        raise RuntimeError("MongoDB is unavailable for secure account storage.") from exc


def get_user_credential(user_id: str, provider: str) -> str | None:
    _require_database()
    document = database.user_credentials.find_one(
        {"user_id": user_id, "provider": provider}, {"encrypted_payload": 1}
    )
    return str(document["encrypted_payload"]) if document and document.get("encrypted_payload") else None


def save_user_credential(user_id: str, provider: str, encrypted_payload: str) -> None:
    _require_database()
    database.user_credentials.replace_one(
        {"user_id": user_id, "provider": provider},
        {"user_id": user_id, "provider": provider, "encrypted_payload": encrypted_payload},
        upsert=True,
    )


def delete_user_credential(user_id: str, provider: str) -> None:
    _require_database()
    database.user_credentials.delete_one({"user_id": user_id, "provider": provider})


def insert_run(document: dict) -> dict:
    normalized = _clean(document)
    if _use_memory():
        _MEMORY_FALLBACK["runs"][normalized["id"]] = normalized
        return normalized
    # Upsert: runs are re-persisted on every status change, so a plain insert
    # would raise DuplicateKeyError from the second write onwards.
    database.runs.replace_one({"id": normalized["id"]}, normalized, upsert=True)
    return normalized


def _owner_filter(owner_id: str | None) -> dict:
    return {"owner_id": owner_id} if owner_id is not None else {}


def list_runs(owner_id: str | None = None) -> list[dict]:
    if _use_memory():
        return [
            run for run in _MEMORY_FALLBACK["runs"].values()
            if owner_id is None or run.get("owner_id") == owner_id
        ]
    return [_clean(doc) for doc in database.runs.find(_owner_filter(owner_id)).sort("created_at", -1)]


def get_run(run_id: str, owner_id: str | None = None) -> dict | None:
    if _use_memory():
        run = _MEMORY_FALLBACK["runs"].get(run_id)
        return run if run is not None and (owner_id is None or run.get("owner_id") == owner_id) else None
    return _clean(database.runs.find_one({"id": run_id, **_owner_filter(owner_id)}))


def list_approvals(owner_id: str | None = None) -> list[dict]:
    if _use_memory():
        return [
            approval for approval in _MEMORY_FALLBACK["approvals"].values()
            if owner_id is None or approval.get("owner_id") == owner_id
        ]
    return [_clean(doc) for doc in database.approvals.find(_owner_filter(owner_id)).sort("created_at", -1)]


def get_approval(approval_id: str, owner_id: str | None = None) -> dict | None:
    if _use_memory():
        approval = _MEMORY_FALLBACK["approvals"].get(approval_id)
        return approval if approval is not None and (owner_id is None or approval.get("owner_id") == owner_id) else None
    return _clean(database.approvals.find_one({"id": approval_id, **_owner_filter(owner_id)}))


def insert_approval(document: dict) -> dict:
    normalized = _clean(document)
    if _use_memory():
        _MEMORY_FALLBACK["approvals"][normalized["id"]] = normalized
        return normalized
    database.approvals.replace_one({"id": normalized["id"]}, normalized, upsert=True)
    return normalized


def update_approval(approval_id: str, update: dict, owner_id: str | None = None) -> dict | None:
    if _use_memory():
        existing = _MEMORY_FALLBACK["approvals"].get(approval_id)
        if existing is None or (owner_id is not None and existing.get("owner_id") != owner_id):
            return None
        existing.update(update)
        _MEMORY_FALLBACK["approvals"][approval_id] = existing
        return existing
    result = database.approvals.find_one_and_update(
        {"id": approval_id, **_owner_filter(owner_id)},
        {"$set": _normalize_value({key: value for key, value in update.items() if key != "_id"})},
        return_document=True,
    )
    return _clean(result)


def list_evaluations(owner_id: str | None = None) -> list[dict]:
    if _use_memory():
        return [
            item for item in _MEMORY_FALLBACK["evaluations"].values()
            if owner_id is None or item.get("owner_id") == owner_id
        ]
    return [_clean(doc) for doc in database.evaluations.find(_owner_filter(owner_id)).sort("created_at", -1)]


def get_evaluation(run_id: str, owner_id: str | None = None) -> dict | None:
    if _use_memory():
        item = _MEMORY_FALLBACK["evaluations"].get(run_id)
        return item if item is not None and (owner_id is None or item.get("owner_id") == owner_id) else None
    return _clean(database.evaluations.find_one({"run_id": run_id, **_owner_filter(owner_id)}))


def delete_run(run_id: str, owner_id: str | None = None) -> None:
    """Delete a run and everything that hangs off it (approvals, evaluation).

    The console lists one card per approval and one entry per evaluation, so
    leaving orphans behind is what made old test runs keep cluttering the UI.
    """
    if _use_memory():
        run = _MEMORY_FALLBACK["runs"].get(run_id)
        if run is not None and (owner_id is None or run.get("owner_id") == owner_id):
            _MEMORY_FALLBACK["runs"].pop(run_id, None)
        else:
            return
        for approval_id in [
            key
            for key, value in _MEMORY_FALLBACK["approvals"].items()
            if value.get("run_id") == run_id
        ]:
            _MEMORY_FALLBACK["approvals"].pop(approval_id, None)
        _MEMORY_FALLBACK["evaluations"].pop(run_id, None)
        return
    query = {"id": run_id, **_owner_filter(owner_id)}
    database.runs.delete_one(query)
    database.approvals.delete_many({"run_id": run_id, **_owner_filter(owner_id)})
    database.evaluations.delete_many({"run_id": run_id, **_owner_filter(owner_id)})


def delete_all_runs(owner_id: str | None = None) -> int:
    """Wipe one account's history, or all history for an unscoped caller."""
    if _use_memory():
        selected = [
            run_id for run_id, run in _MEMORY_FALLBACK["runs"].items()
            if owner_id is None or run.get("owner_id") == owner_id
        ]
        count = len(selected)
        selected_ids = set(selected)
        for run_id in selected:
            _MEMORY_FALLBACK["runs"].pop(run_id, None)
            _MEMORY_FALLBACK["evaluations"].pop(run_id, None)
        for approval_id, approval in list(_MEMORY_FALLBACK["approvals"].items()):
            if approval.get("run_id") in selected_ids or (
                owner_id is not None and approval.get("owner_id") == owner_id
            ) or owner_id is None:
                _MEMORY_FALLBACK["approvals"].pop(approval_id, None)
        for evaluation_id, evaluation in list(_MEMORY_FALLBACK["evaluations"].items()):
            if owner_id is None or evaluation.get("owner_id") == owner_id:
                _MEMORY_FALLBACK["evaluations"].pop(evaluation_id, None)
        return count
    query = _owner_filter(owner_id)
    count = database.runs.count_documents(query)
    database.runs.delete_many(query)
    database.approvals.delete_many(_owner_filter(owner_id))
    database.evaluations.delete_many(_owner_filter(owner_id))
    return count


def insert_evaluation(document: dict) -> dict:
    normalized = _clean(document)
    if _use_memory():
        _MEMORY_FALLBACK["evaluations"][normalized["run_id"]] = normalized
        return normalized
    database.evaluations.replace_one({"run_id": normalized["run_id"]}, normalized, upsert=True)
    return normalized
