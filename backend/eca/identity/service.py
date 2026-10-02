"""Users (Batch A: data model and service functions only; sign-in arrives in Batch B).

``create_user`` runs in a unit of work whose ``app.user_id`` is the new user's ID (the API role's
own-row policy, BACKEND_DESIGN.md §7.6). Tests and the evaluation harness call it; the Batch B
sign-in callback will call the same function. There is no other way to create a user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from eca.identity.events import USER_DELETION_REQUESTED, UserDeletionRequested
from eca.identity.models import deletion_jobs_table, users_table
from eca.platform.errors import ValidationFailed
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork


@dataclass(frozen=True)
class UserSettings:
    user_id: UUID
    timezone: str
    status: str
    work_hours: dict[str, Any] = field(default_factory=dict)  # users.work_hours (coverage, §9.10)


async def create_user(uow: UnitOfWork, *, email: str, display_name: str, timezone: str = "UTC") -> UUID:
    if uow.user_id is None:
        raise ValidationFailed("create_user needs a unit of work for the new user's ID")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationFailed(f"unknown timezone {timezone!r}") from exc
    await uow.session.execute(
        insert(users_table).values(
            id=uow.user_id, email=email.strip().lower(), display_name=display_name, timezone=timezone
        )
    )
    return uow.user_id


async def get_user_settings(uow: UnitOfWork) -> UserSettings:
    """The current user's timezone and status (columns the worker role may read, §7.6)."""
    row = (
        await uow.session.execute(
            select(
                users_table.c.id, users_table.c.timezone, users_table.c.status, users_table.c.work_hours
            ).where(users_table.c.id == uow.user_id)
        )
    ).one()
    return UserSettings(
        user_id=row.id, timezone=row.timezone, status=row.status, work_hours=dict(row.work_hours or {})
    )


async def list_active_user_ids(uow: UnitOfWork) -> list[UUID]:
    """Worker role only: every active user's ID, through ``users_worker_enumerate`` (§7.6)."""
    rows = await uow.session.execute(
        select(users_table.c.id).where(users_table.c.status == "active").order_by(users_table.c.id)
    )
    return [r.id for r in rows]


@dataclass(frozen=True)
class UserProfile:
    user_id: UUID
    email: str
    display_name: str
    timezone: str
    status: str


async def find_signin_user(uow: UnitOfWork, *, sub: str, email: str) -> UUID | None:
    """Pre-authentication lookup by verified Google identity (``eca_signin_user_id``, 0009)."""
    value = (
        await uow.session.execute(
            text("SELECT eca_signin_user_id(:sub, :email)"), {"sub": sub, "email": email}
        )
    ).scalar_one_or_none()
    return None if value is None else UUID(str(value))


async def link_google_identity(uow: UnitOfWork, *, sub: str) -> None:
    await uow.session.execute(
        update(users_table).where(users_table.c.id == uow.user_id).values(google_sub=sub)
    )


async def get_profile(uow: UnitOfWork) -> UserProfile:
    t = users_table
    row = (
        await uow.session.execute(
            select(t.c.id, t.c.email, t.c.display_name, t.c.timezone, t.c.status).where(t.c.id == uow.user_id)
        )
    ).one()
    return UserProfile(row.id, row.email, row.display_name, row.timezone, row.status)


async def request_account_deletion(uow: UnitOfWork) -> UUID:
    """Record the request (job row ``pending``, user ``deleting``, event); idempotent per user."""
    jobs = deletion_jobs_table
    job_id = uuid7()
    inserted = (
        await uow.session.execute(
            pg_insert(jobs)
            .values(id=job_id, user_id=uow.user_id, kind="account", status="pending")
            .on_conflict_do_nothing()
            .returning(jobs.c.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        existing = (
            await uow.session.execute(
                select(jobs.c.id).where(
                    jobs.c.user_id == uow.user_id,
                    jobs.c.kind == "account",
                    jobs.c.status.in_(["pending", "running"]),
                )
            )
        ).scalar_one()
        return UUID(str(existing))
    await uow.session.execute(
        update(users_table).where(users_table.c.id == uow.user_id).values(status="deleting")
    )
    assert uow.user_id is not None
    await publish(
        uow,
        NewEvent(
            USER_DELETION_REQUESTED,
            "user",
            uow.user_id,
            UserDeletionRequested(user_id=uow.user_id, deletion_job_id=job_id),
        ),
    )
    return job_id
