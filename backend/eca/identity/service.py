"""Users (Batch A: data model and service functions only; sign-in arrives in Batch B).

``create_user`` runs in a unit of work whose ``app.user_id`` is the new user's ID (the API role's
own-row policy, BACKEND_DESIGN.md §7.6). Tests and the evaluation harness call it; the Batch B
sign-in callback will call the same function. There is no other way to create a user.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import insert, select

from eca.identity.models import users_table
from eca.platform.errors import ValidationFailed
from eca.platform.uow import UnitOfWork


@dataclass(frozen=True)
class UserSettings:
    user_id: UUID
    timezone: str
    status: str


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
            select(users_table.c.id, users_table.c.timezone, users_table.c.status).where(
                users_table.c.id == uow.user_id
            )
        )
    ).one()
    return UserSettings(user_id=row.id, timezone=row.timezone, status=row.status)


async def list_active_user_ids(uow: UnitOfWork) -> list[UUID]:
    """Worker role only: every active user's ID, through ``users_worker_enumerate`` (§7.6)."""
    rows = await uow.session.execute(
        select(users_table.c.id).where(users_table.c.status == "active").order_by(users_table.c.id)
    )
    return [r.id for r in rows]
