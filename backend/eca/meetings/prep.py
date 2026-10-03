"""Prep storage (slice 4.4, BACKEND_DESIGN.md §18 "Prep cache key and invalidation").

``meetings`` is the single writer of ``meetings.prep_brief``; ``attention`` computes the sections
(deterministic) and stores them here, ``retrieval`` stores AI-11 asks here.

``prep_brief`` = ``{key, computed_at, sections, asks}``; ``prep_brief_version`` increments each
time a different key is stored. A store is conditional on the version read, so concurrent
recomputations keep one result. Asks belong to one version:
``asks = {version, status, items, provenance}`` with status ``not_requested`` → ``pending`` →
``ready`` | ``failed`` | ``skipped``; AI-11 runs at most once per version.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update

from eca.meetings.events import MEETING_ASKS_REQUESTED, MeetingAsksRequested
from eca.meetings.models import meetings_table
from eca.platform.events import NewEvent
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork

ASK_STATES = frozenset({"not_requested", "pending", "ready", "failed", "skipped"})
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended('prep:' || :meeting, 0))")


@dataclass(frozen=True)
class StoredPrep:
    meeting_id: UUID
    version: int  # prep_brief_version
    key: str | None
    computed_at: str | None
    sections: dict[str, Any]
    asks: dict[str, Any]


def _asks(brief: dict[str, Any] | None, version: int) -> dict[str, Any]:
    asks = dict((brief or {}).get("asks") or {})
    if asks.get("version") != version or asks.get("status") not in ASK_STATES:
        return {"version": version, "status": "not_requested", "items": [], "provenance": None}
    return asks


async def lock_prep(uow: UnitOfWork, meeting_id: UUID) -> None:
    await uow.session.execute(_LOCK_SQL, {"meeting": str(meeting_id)})


async def get_prep(uow: UnitOfWork, meeting_id: UUID) -> StoredPrep | None:
    m = meetings_table
    row = (
        await uow.session.execute(
            select(m.c.prep_brief, m.c.prep_brief_version).where(
                m.c.user_id == uow.user_id, m.c.id == meeting_id, m.c.deleted_at.is_(None)
            )
        )
    ).one_or_none()
    if row is None:
        return None
    brief = row.prep_brief or {}
    version = int(row.prep_brief_version)
    return StoredPrep(
        meeting_id=meeting_id,
        version=version,
        key=brief.get("key"),
        computed_at=brief.get("computed_at"),
        sections=dict(brief.get("sections") or {}),
        asks=_asks(brief, version),
    )


async def store_prep_sections(
    uow: UnitOfWork,
    meeting_id: UUID,
    *,
    key: str,
    sections: dict[str, Any],
    computed_at: datetime.datetime,
) -> int:
    """Store the sections for ``key`` (under the ``prep:{meeting}`` lock). An unchanged key is a
    no-op; a new key increments the version and resets the asks. Returns the current version."""
    await lock_prep(uow, meeting_id)
    current = await get_prep(uow, meeting_id)
    if current is None:
        return 0
    if current.key == key:
        return current.version
    m = meetings_table
    new_version = current.version + 1
    updated = (
        await uow.session.execute(
            update(m)
            .where(m.c.id == meeting_id, m.c.prep_brief_version == current.version)
            .values(
                prep_brief={
                    "key": key,
                    "computed_at": computed_at.isoformat(),
                    "sections": sections,
                    "asks": {
                        "version": new_version,
                        "status": "not_requested",
                        "items": [],
                        "provenance": None,
                    },
                },
                prep_brief_version=new_version,
            )
            .returning(m.c.prep_brief_version)
        )
    ).scalar_one_or_none()
    return int(updated) if updated is not None else current.version


async def request_asks(uow: UnitOfWork, meeting_id: UUID, *, version: int) -> StoredPrep | None:
    """Once per version: ``not_requested → pending`` and ``MeetingAsksRequested``. Returns the
    stored prep (asks unchanged when they were already requested for this version)."""
    await lock_prep(uow, meeting_id)
    current = await get_prep(uow, meeting_id)
    if current is None or current.version != version:
        return current
    if current.asks["status"] != "not_requested":
        return current
    await _set_asks(uow, meeting_id, version, {**current.asks, "status": "pending"})
    await publish(
        uow,
        NewEvent(
            MEETING_ASKS_REQUESTED,
            "meeting",
            meeting_id,
            MeetingAsksRequested(meeting_id=meeting_id, version=version),
        ),
    )
    return await get_prep(uow, meeting_id)


async def _set_asks(uow: UnitOfWork, meeting_id: UUID, version: int, asks: dict[str, Any]) -> bool:
    m = meetings_table
    row = (
        await uow.session.execute(
            select(m.c.prep_brief)
            .where(m.c.id == meeting_id, m.c.prep_brief_version == version)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        return False
    brief = dict(row.prep_brief or {})
    brief["asks"] = asks
    await uow.session.execute(
        update(m).where(m.c.id == meeting_id, m.c.prep_brief_version == version).values(prep_brief=brief)
    )
    return True


async def store_prep_asks(
    uow: UnitOfWork,
    meeting_id: UUID,
    *,
    version: int,
    status: str,
    items: list[dict[str, Any]],
    provenance: dict[str, Any] | None,
) -> bool:
    """AI-11 result for one version (``ready``, ``failed`` or ``skipped``); stored only while the
    version still matches and the asks are ``pending``."""
    if status not in ("ready", "failed", "skipped"):
        raise ValueError(f"unknown asks status {status!r}")
    await lock_prep(uow, meeting_id)
    current = await get_prep(uow, meeting_id)
    if current is None or current.version != version or current.asks["status"] != "pending":
        return False
    return await _set_asks(
        uow,
        meeting_id,
        version,
        {"version": version, "status": status, "items": items, "provenance": provenance},
    )
