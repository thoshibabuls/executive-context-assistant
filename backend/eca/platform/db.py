"""Database engine and session factory (SQLAlchemy 2 async on psycopg 3 only)."""

from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_PSYCOPG_DRIVER = "postgresql+psycopg"
_ACCEPTED_SCHEMES = {"postgres", "postgresql", "postgresql+psycopg", "postgresql+asyncpg"}


def to_psycopg_url(url: str) -> str:
    """Normalize any PostgreSQL URL to the psycopg 3 driver.

    asyncpg is not used (single driver, BACKEND_DESIGN.md §2.2); an asyncpg URL from an older
    configuration is rewritten rather than silently loading a second driver.
    """
    scheme, sep, rest = url.partition("://")
    if not sep or scheme not in _ACCEPTED_SCHEMES:
        raise ValueError("Database URL must use a postgresql:// scheme")
    normalized = f"{_PSYCOPG_DRIVER}://{rest}"
    make_url(normalized)  # validates the URL structure
    return normalized


def create_engine(
    url: str, *, pool_size: int = 5, max_overflow: int = 10, connect_timeout_s: int = 5
) -> AsyncEngine:
    return create_async_engine(
        to_psycopg_url(url),
        connect_args={"connect_timeout": connect_timeout_s},
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
