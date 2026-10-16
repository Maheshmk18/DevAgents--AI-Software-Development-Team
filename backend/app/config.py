from functools import lru_cache
from typing import List

from pydantic import Field
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
    allowed_origins: str = Field(default="http://localhost:5173")
    gmail_access_token: str = Field(default="")
    gmail_sender_email: str = Field(default="you@example.com")
    github_token: str = Field(default="")
    #: Groq is the one and only LLM provider for this project.
    #: Free key from https://console.groq.com/keys
    groq_api_key: str = Field(default="")
    groq_model: str = Field(default="llama-3.3-70b-versatile")
    #: Workspace root the repo tools operate on. Empty means "current directory".
    repo_root: str = Field(default="")
    #: When true, agent tool calls are routed through real MCP stdio servers.
    #: When false (or if a server fails to start) the in-process clients are used.
    mcp_enabled: bool = Field(default=True)

    @property
    def allowed_origin_list(self) -> List[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def llm_provider(self) -> str:
        """Which LLM backend is configured: ``groq`` or ``none``."""
        return "groq" if self.groq_api_key else "none"

    @property
    def llm_model(self) -> str:
        """The model name for the active provider."""
        return self.groq_model


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
