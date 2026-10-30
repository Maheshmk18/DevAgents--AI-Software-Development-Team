import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.approval_routes import router as approval_router
from app.api.auth_routes import router as auth_router
from app.api.connectors_routes import router as connectors_router
from app.api.evaluation_routes import router as evaluation_router
from app.api.mcp_routes import router as mcp_router
from app.api.routes import router as api_router
from app.api.tools_routes import router as tools_router
from app.config import settings
from app.db import ensure_indexes, ping_database
from app.services.llm_service import llm_service
from app.services.google_auth_service import SESSION_COOKIE, read_session

app = FastAPI(title=settings.app_name, version="0.1.0", debug=settings.debug)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(auth_router)
app.include_router(approval_router)
app.include_router(connectors_router)
app.include_router(tools_router)
app.include_router(evaluation_router)
app.include_router(mcp_router)


_PUBLIC_API_PATHS = {
    "/api/health",
    "/api/healthcheck",
    "/api/ping",
    "/api/version",
    "/api/hello",
    "/api/auth/google/start",
    "/api/auth/google/callback",
    "/api/auth/me",
    "/api/auth/logout",
}


@app.middleware("http")
async def require_google_session(request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api/") or path in _PUBLIC_API_PATHS:
        return await call_next(request)
    # OAuth callbacks for existing GitHub/Gmail connectors arrive from the
    # provider without an app session; state validation protects those routes.
    if path.startswith("/api/connectors/") and path.endswith("/callback"):
        return await call_next(request)

    origin = request.headers.get("origin")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
        if origin.rstrip("/") not in settings.allowed_origin_list:
            return JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)

    user = read_session(request.cookies.get(SESSION_COOKIE))
    if user is None:
        return JSONResponse({"detail": "Sign in with Google to continue."}, status_code=401)
    request.state.user = user
    return await call_next(request)


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": f"Welcome to {settings.app_name}"}


@app.on_event("startup")
async def startup_event() -> None:
    ensure_indexes()
    db_state = ping_database()
    if db_state.get("status") == "error":
        print(f"MongoDB not reachable: {db_state.get('detail')}")
    # Probe the LLM off the request path: this keeps the console badge honest and
    # means a broken key does not make the first run slow.
    threading.Thread(target=llm_service.probe, name="llm-probe", daemon=True).start()
