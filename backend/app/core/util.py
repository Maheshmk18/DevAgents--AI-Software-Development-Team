from __future__ import annotations

from typing import Any

MAX_TEXT = 2000


def truncate(value: Any, limit: int = MAX_TEXT) -> Any:
    """Keep tool output small enough to store safely in run history."""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [truncate(item, limit) for item in value[:50]]
    if isinstance(value, dict):
        return {key: truncate(item, limit) for key, item in list(value.items())[:20]}
    return value
