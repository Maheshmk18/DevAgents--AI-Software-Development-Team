from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Iterator


_REPOSITORY_ROOT: ContextVar[Path | None] = ContextVar("agent_repository_root", default=None)
_REPOSITORY_SLUG: ContextVar[str | None] = ContextVar("agent_repository_slug", default=None)


def active_repository_root() -> Path | None:
    return _REPOSITORY_ROOT.get()


def active_repository_slug() -> str | None:
    return _REPOSITORY_SLUG.get()


@contextmanager
def agent_run_context(repository_root: str | Path, repository_slug: str) -> Iterator[None]:
    root_token: Token[Path | None] = _REPOSITORY_ROOT.set(Path(repository_root).resolve())
    slug_token: Token[str | None] = _REPOSITORY_SLUG.set(repository_slug)
    try:
        yield
    finally:
        _REPOSITORY_SLUG.reset(slug_token)
        _REPOSITORY_ROOT.reset(root_token)
