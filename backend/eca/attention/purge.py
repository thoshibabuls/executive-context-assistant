"""Deletion and retention in ``attention`` (BACKEND_DESIGN.md §13.3, §17.6).

Account deletion runs after ``chat``: notifications (they reference reminders), reminders, push
subscriptions. Retention (nightly, per user): notifications older than 30 days, reminders closed
more than 90 days ago (and no longer referenced by a notification), briefings older than 30 days,
priority pairs older than 180 days.
"""

from __future__ import annotations

import datetime

from sqlalchemy import delete, select

from eca.attention.models import (
    briefings_table,
    notifications_table,
    priority_pairs_table,
    push_subscriptions_table,
    reminders_table,
    user_priority_weights_table,
)
from eca.platform.uow import UnitOfWork

NOTIFICATION_DAYS = 30
CLOSED_REMINDER_DAYS = 90
BRIEFING_DAYS = 30
PAIR_DAYS = 180
TERMINAL = ("dismissed", "acted", "suppressed", "cancelled")


async def purge_user(uow: UnitOfWork) -> None:
    n, r, s = notifications_table, reminders_table, push_subscriptions_table
    await uow.session.execute(delete(n).where(n.c.user_id == uow.user_id))
    await uow.session.execute(delete(r).where(r.c.user_id == uow.user_id))
    await uow.session.execute(delete(s).where(s.c.user_id == uow.user_id))
    await uow.session.execute(delete(briefings_table).where(briefings_table.c.user_id == uow.user_id))
    await uow.session.execute(
        delete(priority_pairs_table).where(priority_pairs_table.c.user_id == uow.user_id)
    )
    await uow.session.execute(
        delete(user_priority_weights_table).where(user_priority_weights_table.c.user_id == uow.user_id)
    )


async def purge_expired(uow: UnitOfWork, *, now: datetime.datetime) -> int:
    n, r = notifications_table, reminders_table
    deleted = await uow.session.execute(
        delete(n).where(
            n.c.user_id == uow.user_id, n.c.created_at < now - datetime.timedelta(days=NOTIFICATION_DAYS)
        )
    )
    referenced = select(n.c.reminder_id).where(n.c.user_id == uow.user_id)
    closed = await uow.session.execute(
        delete(r).where(
            r.c.user_id == uow.user_id,
            r.c.state.in_(TERMINAL),
            r.c.closed_at < now - datetime.timedelta(days=CLOSED_REMINDER_DAYS),
            r.c.id.not_in(referenced),
        )
    )
    b = briefings_table
    old = await uow.session.execute(
        delete(b).where(
            b.c.user_id == uow.user_id, b.c.generated_at < now - datetime.timedelta(days=BRIEFING_DAYS)
        )
    )
    pp = priority_pairs_table
    pairs = await uow.session.execute(
        delete(pp).where(
            pp.c.user_id == uow.user_id, pp.c.created_at < now - datetime.timedelta(days=PAIR_DAYS)
        )
    )
    return int(deleted.rowcount) + int(closed.rowcount) + int(old.rowcount) + int(pairs.rowcount)  # type: ignore[attr-defined]
