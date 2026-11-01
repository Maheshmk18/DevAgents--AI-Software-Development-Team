from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Iterator


_REPOSITORY_ROOT: ContextVar[Path | None] = ContextVar("agent_repository_root", default=None)
_REPOSITORY_SLUG: ContextVar[str | None] = ContextVar("agent_repository_slug", default=None)
_USER_ID: ContextVar[str | None] = ContextVar("agent_user_id", default=None)


def active_repository_root() -> Path | None:
    return _REPOSITORY_ROOT.get()


def active_repository_slug() -> str | None:
    return _REPOSITORY_SLUG.get()


def active_user_id() -> str | None:
    return _USER_ID.get()


@contextmanager
def agent_run_context(
    repository_root: str | Path | None = None,
    repository_slug: str | None = None,
    user_id: str | None = None,
) -> Iterator[None]:
    root = Path(repository_root).resolve() if repository_root is not None else None
    root_token: Token[Path | None] = _REPOSITORY_ROOT.set(root)
    slug_token: Token[str | None] = _REPOSITORY_SLUG.set(repository_slug)
    user_token: Token[str | None] = _USER_ID.set(user_id)
    try:
        yield
    finally:
        _USER_ID.reset(user_token)
        _REPOSITORY_SLUG.reset(slug_token)
        _REPOSITORY_ROOT.reset(root_token)
