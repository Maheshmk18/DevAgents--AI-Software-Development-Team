import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.approval_routes import router as approval_router
from app.api.connectors_routes import router as connectors_router
from app.api.evaluation_routes import router as evaluation_router
from app.api.mcp_routes import router as mcp_router
from app.api.routes import router as api_router
from app.api.tools_routes import router as tools_router
from app.config import settings
from app.db import ensure_indexes, ping_database
from app.services.llm_service import llm_service

app = FastAPI(title=settings.app_name, version="0.1.0", debug=settings.debug)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(approval_router)
app.include_router(connectors_router)
app.include_router(tools_router)
app.include_router(evaluation_router)
app.include_router(mcp_router)


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
