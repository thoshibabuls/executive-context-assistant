"""``user_checkpoints`` (CONTEXT_ARCHITECTURE.md §5.2 A1, §7.1, §10.11). USER-AUTHORED: a surface
was seen at a time. The later timestamp always wins (BACKEND_DESIGN.md §6.2), so a delayed or
repeated request never moves a checkpoint backwards.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence

from sqlalchemy import Column, DateTime, MetaData, Table, Text, delete, func, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert

from eca.platform.errors import ValidationFailed
from eca.platform.uow import UnitOfWork

SURFACE = re.compile(r"^(today|chat|(person|project|meeting):[0-9a-f-]{36})$")

metadata = MetaData()
user_checkpoints_table = Table(
    "user_checkpoints",
    metadata,
    Column("user_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("surface", Text, primary_key=True),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True)),
)


def check_surface(surface: str) -> str:
    if not SURFACE.fullmatch(surface):
        raise ValidationFailed("surface must be today, chat, person:<id>, project:<id> or meeting:<id>")
    return surface


async def mark_seen(uow: UnitOfWork, surface: str, *, at: datetime.datetime) -> datetime.datetime:
    """Upsert the checkpoint; returns the stored (latest) timestamp."""
    t = user_checkpoints_table
    stmt = insert(t).values(user_id=uow.user_id, surface=check_surface(surface), last_seen_at=at)
    row = (
        await uow.session.execute(
            stmt.on_conflict_do_update(
                index_elements=["user_id", "surface"],
                set_={
                    "last_seen_at": func.greatest(t.c.last_seen_at, stmt.excluded.last_seen_at),
                    "updated_at": func.now(),
                },
            ).returning(t.c.last_seen_at)
        )
    ).one()
    stored: datetime.datetime = row.last_seen_at
    return stored


async def get_checkpoints(uow: UnitOfWork, surfaces: Sequence[str]) -> dict[str, datetime.datetime]:
    t = user_checkpoints_table
    rows = await uow.session.execute(
        select(t.c.surface, t.c.last_seen_at).where(
            t.c.user_id == uow.user_id, t.c.surface.in_(list(surfaces))
        )
    )
    return {r.surface: r.last_seen_at for r in rows}


async def purge_checkpoints(uow: UnitOfWork) -> None:
    t = user_checkpoints_table
    await uow.session.execute(delete(t).where(t.c.user_id == uow.user_id))
