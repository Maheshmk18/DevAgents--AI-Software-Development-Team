from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.services.google_auth_service import (
    OAUTH_STATE_COOKIE,
    OAUTH_TTL_SECONDS,
    OAUTH_VERIFIER_COOKIE,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    GoogleAuthError,
    begin_google_sign_in,
    create_session,
    finish_google_sign_in,
    read_session,
)

router = APIRouter(prefix="/api/auth", tags=["authentication"])
_logger = logging.getLogger(__name__)


def _secure_cookie() -> bool:
    return settings.backend_public_url.lower().startswith("https://")


@router.get("/google/start")
async def google_sign_in_start() -> RedirectResponse:
    try:
        authorization_url, state, verifier = begin_google_sign_in()
    except GoogleAuthError as exc:
        return RedirectResponse(
            f"{settings.frontend_public_url.rstrip('/')}/signin?{urlencode({'error': str(exc)})}",
            status_code=303,
        )

    response = RedirectResponse(authorization_url, status_code=302)
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=OAUTH_TTL_SECONDS,
        httponly=True,
        secure=_secure_cookie(),
        samesite="lax",
        path="/api/auth/google/callback",
    )
    response.set_cookie(
        OAUTH_VERIFIER_COOKIE,
        verifier,
        max_age=OAUTH_TTL_SECONDS,
        httponly=True,
        secure=_secure_cookie(),
        samesite="lax",
        path="/api/auth/google/callback",
    )
    return response


@router.get("/google/callback")
async def google_sign_in_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str = Query(default=""),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    frontend = settings.frontend_public_url.rstrip("/")
    try:
        if error:
            raise GoogleAuthError("Google sign-in was cancelled or denied.")
        user = finish_google_sign_in(
            code or "",
            state,
            request.cookies.get(OAUTH_STATE_COOKIE, ""),
            request.cookies.get(OAUTH_VERIFIER_COOKIE, ""),
        )
        session = create_session(user)
    except GoogleAuthError as exc:
        _logger.info("Google sign-in did not complete: %s", exc)
        response = RedirectResponse(
            f"{frontend}/signin?{urlencode({'error': str(exc)})}", status_code=303
        )
    except Exception:
        _logger.exception("Google sign-in callback failed")
        response = RedirectResponse(f"{frontend}/signin?error=Google%20sign-in%20failed", status_code=303)
    else:
        response = RedirectResponse(f"{frontend}/app", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=_secure_cookie(),
            samesite="lax",
            path="/",
        )
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/api/auth/google/callback")
    response.delete_cookie(OAUTH_VERIFIER_COOKIE, path="/api/auth/google/callback")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/me")
async def current_user(request: Request) -> dict[str, object]:
    user = read_session(request.cookies.get(SESSION_COOKIE))
    return {"authenticated": user is not None, "user": user}


@router.post("/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"status": "signed_out"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
