from functools import lru_cache
from typing import List
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="AI Dev Team")
    debug: bool = Field(default=False)
    mongo_uri: str = Field(default="mongodb://localhost:27017")
    mongo_db: str = Field(default="ai_dev_team")
    allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173"
    )
    gmail_access_token: str = Field(default="")
    #: Preferred Gmail credential. A refresh token (with the OAuth client id and
    #: secret below) is exchanged for short-lived access tokens automatically, so
    #: sending mail keeps working instead of dying when a pasted token expires.
    gmail_refresh_token: str = Field(default="")
    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")
    gmail_sender_email: str = Field(default="you@example.com")
    github_token: str = Field(default="")
    #: OAuth App credentials are configured once by the console operator. User
    #: access tokens are obtained with OAuth and stored in the OS credential store.
    github_oauth_client_id: str = Field(default="")
    github_oauth_client_secret: str = Field(default="")
    #: Default GitHub repository ("owner/name") the agents operate on. When set,
    #: the github.* tools fill in ``owner``/``repo`` if the model omits them, and
    #: the schemas advertise it so the model does not invent placeholders.
    github_repo: str = Field(default="")
    #: Groq is the one and only LLM provider for this project.
    #: Free key from https://console.groq.com/keys
    groq_api_key: str = Field(default="")
    groq_model: str = Field(default="qwen/qwen3.8-27b")
    #: Workspace root the repo tools operate on. Empty means "current directory".
    repo_root: str = Field(default="")
    #: Public URL used in OAuth callback URIs. Keep this aligned with the
    #: callback URLs configured in the GitHub and Google OAuth applications.
    backend_public_url: str = Field(default="http://localhost:8002")
    #: When true, agent tool calls are routed through real MCP stdio servers.
    #: When false (or if a server fails to start) the in-process clients are used.
    mcp_enabled: bool = Field(default=True)

    @field_validator("debug", mode="before")
    @classmethod
    def normalize_debug_mode(cls, value: object) -> object:
        """Accept common deployment profiles from host environments.

        Some launchers export ``DEBUG=release`` instead of a boolean. Treat a
        release/production profile as debug-disabled so it cannot prevent the
        backend from starting; preserve Pydantic's normal boolean validation
        for all other values.
        """
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"release", "production", "prod"}:
                return False
            if normalized in {"debug", "development", "dev"}:
                return True
        return value

    @property
    def allowed_origin_list(self) -> List[str]:
        origins = [item.strip() for item in self.allowed_origins.split(",") if item.strip()]
        # Local dev servers are commonly opened as either localhost or
        # 127.0.0.1. Keep both loopback origins available for a local backend,
        # even when an older .env lists only one of them.
        backend_host = urlsplit(self.backend_public_url).hostname
        if backend_host in {"localhost", "127.0.0.1"}:
            # Vite moves to the next available port when its configured port
            # is occupied (for example, 5174 when 5173 is already in use).
            for port in range(5173, 5180):
                origins.extend(
                    (f"http://localhost:{port}", f"http://127.0.0.1:{port}")
                )
        return list(dict.fromkeys(origins))

    @property
    def llm_provider(self) -> str:
        """Which LLM backend is configured: ``groq`` or ``none``."""
        return "groq" if self.groq_api_key else "none"

    @property
    def llm_model(self) -> str:
        """The model name for the active provider."""
        return self.groq_model

    @property
    def gmail_configured(self) -> bool:
        """Whether Gmail has any usable credential at all."""
        refresh_flow = bool(
            self.gmail_refresh_token and self.google_client_id and self.google_client_secret
        )
        return refresh_flow or bool(self.gmail_access_token)

    @property
    def gmail_mode(self) -> str:
        """``refresh_token``, ``access_token``, or ``none``."""
        if self.gmail_refresh_token and self.google_client_id and self.google_client_secret:
            return "refresh_token"
        if self.gmail_access_token:
            return "access_token"
        return "none"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
