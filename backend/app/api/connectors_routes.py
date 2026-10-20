from __future__ import annotations

import json
from html import escape
import logging
from typing import cast

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.integrations.errors import IntegrationError
from app.integrations.github import GitHubClient
from app.services import credential_store
from app.services.connector_service import (
    ConnectorError,
    Provider,
    begin_connection,
    disconnect,
    finish_connection,
    validate_callback_state,
)
from app.services.credential_store import CredentialStoreError

router = APIRouter(prefix="/api/connectors", tags=["connectors"])
_PROVIDERS = frozenset({"github", "gmail"})
_logger = logging.getLogger(__name__)


class GitHubRepositorySelection(BaseModel):
    slug: str = Field(..., min_length=3, max_length=200, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _provider(value: str) -> Provider:
    if value not in _PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown connector: {value}")
    return cast(Provider, value)


@router.post("/{provider}/start")
async def start_connection(provider: str) -> dict[str, str]:
    selected = _provider(provider)
    try:
        return begin_connection(selected)
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ConnectorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/github/repositories")
async def list_github_repositories() -> dict[str, object]:
    try:
        return await run_in_threadpool(GitHubClient().list_repositories)
    except IntegrationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/github/repository")
async def select_github_repository(selection: GitHubRepositorySelection) -> dict[str, str]:
    try:
        credentials = credential_store.get_credentials("github")
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not (credentials or {}).get("access_token") and not settings.github_token:
        raise HTTPException(status_code=409, detail="Connect GitHub before selecting a repository.")
    owner, repo = selection.slug.split("/", 1)
    try:
        repository = await run_in_threadpool(GitHubClient().get_repository, owner, repo)
    except IntegrationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    canonical = f"{repository['owner']}/{repository['repo']}"
    credentials = credentials or {}
    credentials["default_repo"] = canonical
    try:
        await run_in_threadpool(credential_store.save_credentials, "github", credentials)
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"slug": canonical, "status": "selected"}
def _callback_page(provider: Provider, ok: bool, message: str) -> HTMLResponse:
    # The provider and text are server-generated; escape the script terminator
    # defensively before embedding the JSON message in this tiny popup page.
    allowed_origins = json.dumps(settings.allowed_origin_list).replace("</", "<\\/")
    start_path = json.dumps(f"/api/connectors/{provider}/start").replace("</", "<\\/")
    payload = json.dumps(
        {
            "type": "ai-dev-team-connector-result",
            "provider": provider,
            "ok": ok,
            "message": message,
        }
    ).replace("</", "<\\/")
    content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Connector setup</title>
<style>
body {{ margin: 0; padding: 40px 20px; color: #172033; background: #f5f7fb; font: 16px/1.5 system-ui, sans-serif; }}
main {{ max-width: 560px; margin: 8vh auto; padding: 28px; background: white; border: 1px solid #dbe2ee; border-radius: 16px; box-shadow: 0 12px 36px #17203312; }}
h1 {{ margin: 0 0 12px; font-size: 21px; }}
#result {{ color: #536178; overflow-wrap: anywhere; }}
button {{ margin-top: 12px; padding: 10px 16px; border: 0; border-radius: 9px; color: white; background: #4f46e5; font: inherit; font-weight: 600; cursor: pointer; }}
button:disabled {{ opacity: .65; cursor: wait; }}
</style></head><body><main>
<h1 id="title">{escape(f"{provider.title()} connected" if ok else f"Could not connect {provider.title()}")}</h1>
<p id="result" role="status">{escape(message)}</p>
<button id="retry" type="button" hidden>Start connection again</button>
</main>
<script>
const result = {payload};
const allowedOrigins = {allowed_origins};
if (window.opener) {{
  for (const origin of allowedOrigins) window.opener.postMessage(result, origin);
  if (result.ok) window.close();
}}
if (!result.ok) {{
  const retry = document.getElementById('retry');
  retry.hidden = false;
  retry.addEventListener('click', async () => {{
    retry.disabled = true;
    retry.textContent = 'Starting…';
    try {{
      const response = await fetch({start_path}, {{ method: 'POST' }});
      const body = await response.json();
      if (!response.ok || !body.authorization_url) throw new Error(body.detail || 'Could not start authorization.');
      window.location.assign(body.authorization_url);
    }} catch (error) {{
      document.getElementById('result').textContent = error instanceof Error ? error.message : 'Could not start authorization.';
      retry.disabled = false;
      retry.textContent = 'Try again';
    }}
  }});
}}
</script></body></html>"""
    return HTMLResponse(
        content,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{provider}/callback", response_class=HTMLResponse)
async def finish_connection_callback(
    provider: str,
    code: str | None = Query(default=None),
    state: str = Query(default=""),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    selected = _provider(provider)
    if error:
        try:
            validate_callback_state(selected, state)
        except ConnectorError:
            return _callback_page(selected, False, "The authorization response expired. Start again from the console.")
        return _callback_page(selected, False, "Authorization was cancelled.")
    try:
        result = await run_in_threadpool(finish_connection, selected, code or "", state)
    except (ConnectorError, CredentialStoreError) as exc:
        # Keep provider error details and tokens out of the browser callback URL.
        _logger.exception("OAuth callback failed for %s", selected)
        return _callback_page(
            selected,
            False,
            f"Could not finish the connection: {exc}",
        )
    return _callback_page(selected, True, f"Connected {selected} account {result['account']}.")


@router.delete("/{provider}")
async def disconnect_connector(provider: str) -> dict[str, object]:
    selected = _provider(provider)
    try:
        revoked = await run_in_threadpool(disconnect, selected)
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"provider": selected, "status": "disconnected", "provider_revoked": revoked}
