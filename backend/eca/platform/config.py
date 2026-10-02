"""Application settings read from environment variables and the repository ``.env``.

Variable names match the existing ``.env`` (``API_*``, ``GEMINI_API_KEY``). Secrets are held
as ``SecretStr`` so they are never rendered in logs or reprs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    api_env: str = "development"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_cors_origins: str = ""
    api_log_level: str = "INFO"

    # Runtime connections (non-superuser roles subject to RLS) and migration connection (schema owner).
    # The API process uses the API role, the worker process the worker role (BACKEND_DESIGN.md §7.6).
    api_database_url: SecretStr | None = None
    api_worker_database_url: SecretStr | None = None
    api_migration_database_url: SecretStr | None = None
    api_db_runtime_role: str = "eca_app"
    api_db_worker_role: str = "eca_worker"

    sentry_dsn: SecretStr | None = None

    # AI provider layer (BACKEND_DESIGN.md §5.5). The key is needed only in live or record mode.
    gemini_api_key: SecretStr | None = None
    api_ai_mode: Literal["live", "replay", "record"] = "live"
    api_ai_config_dir: str | None = None  # default: <repo>/config
    api_ai_cassette_dir: str | None = None  # required in replay and record mode
    api_ai_timeout_s: float = 60.0

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.api_env.lower() in {"production", "prod"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
