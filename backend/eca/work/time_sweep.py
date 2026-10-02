"""Hourly time sweep (CONTEXT_ARCHITECTURE.md §7.3, §12.4, §12.7; BACKEND_DESIGN.md §9.10, §15).

Per user, one transaction: ``became_overdue``, ``due_soon`` and ``became_stale`` events
(``actor = time``, authority 1, materiality 2) through ``append_event``. The dedupe key holds the
UTC hour bucket of the transition time, so reruns, overlapping sweeps and late sweeps write each
transition once. ``stale`` is a projection column written without a version bump, and cleared
when activity resumes. Time events set no field and do not count as activity in the fold.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import select, update

from eca.identity import list_active_user_ids
from eca.platform.jobs import PeriodicTaskSpec
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory
from eca.work.models import work_items_table
from eca.work.service import append_event

log = structlog.get_logger("eca.work.time_sweep")

TIME_SWEEP_TASK = "eca.work.time_sweep"
DUE_SOON = datetime.timedelta(hours=24)
STALE_AFTER = datetime.timedelta(days=7)
WAITING_STALE_WORKING_DAYS = 5
TIME_MATERIALITY = 2


@dataclass
class SweepReport:
    overdue: int = 0
    due_soon: int = 0
    stale: int = 0
    cleared: int = 0


def hour_bucket(moment: datetime.datetime) -> datetime.datetime:
    return moment.astimezone(datetime.UTC).replace(minute=0, second=0, microsecond=0)


def time_dedupe_key(item_id: UUID, event_type: str, transition: datetime.datetime) -> bytes:
    return hashlib.sha256(
        f"time:{item_id}:{event_type}:{hour_bucket(transition).isoformat()}".encode()
    ).digest()


def add_working_days(start: datetime.datetime, days: int) -> datetime.datetime:
    """``days`` working days (Monday-Friday, UTC calendar) after ``start``."""
    moment = start
    left = days
    while left > 0:
        moment += datetime.timedelta(days=1)
        if moment.weekday() < 5:
            left -= 1
    return moment


def stale_crossing(
    *,
    direction: str,
    due_at: datetime.datetime | None,
    last_activity_at: datetime.datetime | None,
    now: datetime.datetime,
) -> datetime.datetime | None:
    """When the stale policy was crossed (§11.5, §11.6), or None if it is not crossed."""
    crossings = []
    if last_activity_at is not None:
        crossings.append(last_activity_at + STALE_AFTER)
    if direction in ("waiting_for", "delegated") and due_at is not None and due_at < now:
        since = max(due_at, last_activity_at) if last_activity_at else due_at
        crossings.append(add_working_days(since, WAITING_STALE_WORKING_DAYS))
    due = [c for c in crossings if c <= now]
    return min(due) if due else None


async def _emit(uow: UnitOfWork, item_id: UUID, event_type: str, transition: datetime.datetime) -> bool:
    result = await append_event(
        uow,
        item_id=item_id,
        event_type=event_type,
        actor="time",
        authority=1,
        materiality=TIME_MATERIALITY,
        occurred_at=transition,
        dedupe_key=time_dedupe_key(item_id, event_type, transition),
        payload={"transition_at": transition.isoformat()},
    )
    return result.applied


async def sweep_user(uow: UnitOfWork, *, now: datetime.datetime) -> SweepReport:
    t = work_items_table
    rows = (
        await uow.session.execute(
            select(t.c.id, t.c.direction, t.c.due_at, t.c.due_precision, t.c.last_activity_at, t.c.stale)
            .where(
                t.c.user_id == uow.user_id,
                t.c.lifecycle_status.in_(["open", "in_progress"]),
                t.c.verification_status != "rejected",
                t.c.merged_into_id.is_(None),
                t.c.deleted_at.is_(None),
                ~t.c.archived,
            )
            .order_by(t.c.id)
        )
    ).all()
    report = SweepReport()
    for r in rows:
        if r.due_at is not None and r.due_precision != "fuzzy":
            if r.due_at <= now:
                report.overdue += await _emit(uow, r.id, "became_overdue", r.due_at)
            elif r.due_at <= now + DUE_SOON:
                report.due_soon += await _emit(uow, r.id, "due_soon", r.due_at - DUE_SOON)
        crossed = stale_crossing(
            direction=r.direction, due_at=r.due_at, last_activity_at=r.last_activity_at, now=now
        )
        if crossed is not None:
            report.stale += await _emit(uow, r.id, "became_stale", crossed)
            if not r.stale:
                await uow.session.execute(update(t).where(t.c.id == r.id).values(stale=True))
        elif r.stale:
            await uow.session.execute(update(t).where(t.c.id == r.id).values(stale=False))
            report.cleared += 1
    return report


async def sweep_all(uow_factory: UnitOfWorkFactory, *, now: datetime.datetime) -> SweepReport:
    """Every active user, one transaction each (worker role; users through §7.6's policy)."""
    async with uow_factory(user_id=None) as uow:
        users = await list_active_user_ids(uow)
    total = SweepReport()
    for user_id in users:
        async with uow_factory(user_id=user_id) as uow:
            r = await sweep_user(uow, now=now)
        total.overdue += r.overdue
        total.due_soon += r.due_soon
        total.stale += r.stale
        total.cleared += r.cleared
    log.info(
        "time_sweep", users=len(users), overdue=total.overdue, due_soon=total.due_soon, stale=total.stale
    )
    return total


async def _run(uow_factory: UnitOfWorkFactory, now: datetime.datetime) -> None:
    await sweep_all(uow_factory, now=now)


def periodic_tasks() -> list[PeriodicTaskSpec]:
    return [
        PeriodicTaskSpec(
            name=TIME_SWEEP_TASK, periodic_id="time_sweep", cron="5 * * * *", queue="schedule", run=_run
        )
    ]
