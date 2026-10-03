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

    # Google OAuth (slices 1.1 sign-in and 1.2 connect). Secrets only from the environment.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_signin_redirect_uri: str = "http://localhost:8000/api/v1/auth/google/callback"
    google_connect_redirect_uri: str = "http://localhost:8000/api/v1/connections/google/callback"
    web_base_url: str = "http://localhost:3000"

    # Sessions and CSRF (slice 1.1).
    session_cookie_name: str = "eca_session"
    csrf_cookie_name: str = "eca_csrf"
    session_ttl_days: int = 14
    session_cookie_secure: bool = True
    reauth_max_age_minutes: int = 10  # DELETE /me needs a sign-in this recent

    # Refresh-token envelope encryption (slice 1.2): base64 32-byte key-encryption key.
    token_kek: SecretStr | None = None
    token_kek_version: int = 1

    # Pagination cursors (slice 1.7, §16.4): HMAC key. Unset → a random per-process key (cursors
    # then stop working across restarts and instances; set it in deployed environments).
    cursor_signing_key: SecretStr | None = None

    # Web Push (slice 3.1, TECHNICAL_DESIGN.md §15.4): VAPID keys from the environment only
    # (``eca ops vapid-keys`` creates a pair). Unset → Web Push disabled, in-app notifications only.
    web_push_vapid_public_key: str | None = None
    web_push_vapid_private_key: SecretStr | None = None
    web_push_vapid_subject: str | None = None  # mailto: or https: contact

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.api_env.lower() in {"production", "prod"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
