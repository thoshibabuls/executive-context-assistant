"""Meetings from calendar events (slice 1.6, BACKEND_DESIGN.md §9.5).

``upsert_from_source`` runs for a ``calendar_event`` source item in stage ``fetched``: it upserts
the meeting (one per source item), resolves organizer and attendees through ``people`` (unknown
attendees become persons), diffs participants (removed attendees are deleted, response statuses
updated), marks cancellations, moves the stage to ``normalized`` and publishes ``MeetingChanged``.
Calendar fields are SOURCE data: no AI call.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from eca.ingestion import get_source_item, set_stage
from eca.meetings.events import MEETING_CHANGED, MeetingChanged
from eca.meetings.models import meeting_participants_table, meetings_table
from eca.people import get_self_person, resolve_address
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork


@dataclass(frozen=True)
class MeetingView:
    id: UUID
    title: str | None
    starts_at: datetime.datetime
    ends_at: datetime.datetime
    status: str
    conference_uri: str | None
    attendee_ids: tuple[UUID, ...]


async def upsert_from_source(uow: UnitOfWork, source_item_id: UUID) -> UUID | None:
    item = await get_source_item(uow, source_item_id, for_update=True)
    if item.kind != "calendar_event" or item.stage != "fetched" or item.content is None:
        return None
    c: dict[str, Any] = item.content
    seen_at = datetime.datetime.fromisoformat(c["start"])
    organizer_id = None
    if c.get("organizer"):
        organizer = await resolve_address(
            uow,
            email=c["organizer"]["email"],
            display_name=c["organizer"].get("display_name"),
            seen_at=seen_at,
        )
        organizer_id = organizer.id
    status = "cancelled" if c.get("status") == "cancelled" else "scheduled"
    m = meetings_table
    values = {
        "title": c.get("title"),
        "description": c.get("description"),
        "starts_at": datetime.datetime.fromisoformat(c["start"]),
        "ends_at": datetime.datetime.fromisoformat(c["end"]),
        "timezone": c.get("timezone"),
        "series_key": c.get("series_external_id") or c.get("ical_uid"),
        "conference_uri": c.get("conference_uri"),
        "organizer_person_id": organizer_id,
        "status": status,
    }
    row = (
        await uow.session.execute(
            insert(m)
            .values(
                id=uuid7(),
                user_id=uow.user_id,
                source_item_id=item.id,
                processing_status="none",
                version=1,
                **values,
            )
            .on_conflict_do_update(
                index_elements=["source_item_id"], set_={**values, "version": m.c.version + 1}
            )
            .returning(m.c.id, m.c.version)
        )
    ).one()
    meeting_id, version = row.id, row.version
    self_p = await get_self_person(uow)
    keep: dict[UUID, str | None] = {}
    for a in c.get("attendees") or []:
        person = await resolve_address(
            uow, email=a["email"], display_name=a.get("display_name"), seen_at=seen_at
        )
        keep[person.id] = a.get("response")
    if organizer_id is not None:
        keep.setdefault(organizer_id, "accepted")
    keep.setdefault(self_p.id, None)
    mp = meeting_participants_table
    await uow.session.execute(
        delete(mp).where(mp.c.meeting_id == meeting_id, mp.c.person_id.not_in(list(keep)))
    )
    for person_id, response in sorted(keep.items(), key=lambda kv: str(kv[0])):
        await uow.session.execute(
            insert(mp)
            .values(
                meeting_id=meeting_id,
                person_id=person_id,
                user_id=uow.user_id,
                response_status=response,
                is_organizer=person_id == organizer_id,
                origin="source",
            )
            .on_conflict_do_update(
                index_elements=["meeting_id", "person_id"],
                set_={"response_status": response, "is_organizer": person_id == organizer_id},
            )
        )
    await set_stage(uow, item.id, expected=("fetched",), new="normalized")
    await publish(
        uow,
        NewEvent(
            MEETING_CHANGED,
            "meeting",
            meeting_id,
            MeetingChanged(meeting_id=meeting_id, status=status, version=version),
        ),
    )
    return UUID(str(meeting_id))


async def meetings_between(
    uow: UnitOfWork, start: datetime.datetime, end: datetime.datetime, *, include_cancelled: bool = False
) -> list[MeetingView]:
    m, mp = meetings_table, meeting_participants_table
    stmt = select(m.c.id, m.c.title, m.c.starts_at, m.c.ends_at, m.c.status, m.c.conference_uri).where(
        m.c.starts_at < end, m.c.ends_at > start, m.c.deleted_at.is_(None)
    )
    if not include_cancelled:
        stmt = stmt.where(m.c.status != "cancelled")
    rows = (await uow.session.execute(stmt.order_by(m.c.starts_at, m.c.id))).all()
    out = []
    for r in rows:
        people = await uow.session.execute(select(mp.c.person_id).where(mp.c.meeting_id == r.id))
        out.append(
            MeetingView(
                r.id,
                r.title,
                r.starts_at,
                r.ends_at,
                r.status,
                r.conference_uri,
                tuple(sorted(p.person_id for p in people)),
            )
        )
    return out


async def upcoming_attendee_ids(
    uow: UnitOfWork, now: datetime.datetime, horizon: datetime.timedelta
) -> set[UUID]:
    """Persons in meetings starting within ``horizon`` (priority meeting-proximity feature)."""
    return {p for meeting in await meetings_between(uow, now, now + horizon) for p in meeting.attendee_ids}


async def cancel_from_deleted_source(uow: UnitOfWork, source_item_id: UUID) -> None:
    m = meetings_table
    await uow.session.execute(
        update(m)
        .where(m.c.source_item_id == source_item_id)
        .values(status="cancelled", version=m.c.version + 1)
    )


async def purge_user(uow: UnitOfWork) -> None:
    await uow.session.execute(delete(meeting_participants_table))
    await uow.session.execute(delete(meetings_table))


async def purge_sources(uow: UnitOfWork, source_item_ids: list[UUID]) -> None:
    m, mp = meetings_table, meeting_participants_table
    ids = select(m.c.id).where(m.c.source_item_id.in_(source_item_ids))
    await uow.session.execute(delete(mp).where(mp.c.meeting_id.in_(ids)))
    await uow.session.execute(delete(m).where(m.c.source_item_id.in_(source_item_ids)))


@dataclass(frozen=True)
class MeetingDetail:
    """A meeting with what indexing and retrieval read (Phase 2): calendar fields are SOURCE data."""

    id: UUID
    source_item_id: UUID
    title: str | None
    description: str | None
    starts_at: datetime.datetime
    ends_at: datetime.datetime
    status: str
    series_key: str | None
    project_hint: str | None
    attendee_ids: tuple[UUID, ...]


async def _details(uow: UnitOfWork, rows: list[Any]) -> list[MeetingDetail]:
    if not rows:
        return []
    mp = meeting_participants_table
    people: dict[UUID, list[UUID]] = {r.id: [] for r in rows}
    for p in await uow.session.execute(
        select(mp.c.meeting_id, mp.c.person_id)
        .where(mp.c.user_id == uow.user_id, mp.c.meeting_id.in_(list(people)))
        .order_by(mp.c.meeting_id, mp.c.person_id)
    ):
        people[p.meeting_id].append(p.person_id)
    return [
        MeetingDetail(
            r.id,
            r.source_item_id,
            r.title,
            r.description,
            r.starts_at,
            r.ends_at,
            r.status,
            r.series_key,
            r.project_hint,
            tuple(people[r.id]),
        )
        for r in rows
    ]


_DETAIL_COLS = (
    meetings_table.c.id,
    meetings_table.c.source_item_id,
    meetings_table.c.title,
    meetings_table.c.description,
    meetings_table.c.starts_at,
    meetings_table.c.ends_at,
    meetings_table.c.status,
    meetings_table.c.series_key,
    meetings_table.c.project_hint,
)


async def get_meeting_details(uow: UnitOfWork, meeting_ids: list[UUID]) -> list[MeetingDetail]:
    m = meetings_table
    if not meeting_ids:
        return []
    rows = (
        await uow.session.execute(
            select(*_DETAIL_COLS)
            .where(m.c.user_id == uow.user_id, m.c.id.in_(meeting_ids), m.c.deleted_at.is_(None))
            .order_by(m.c.starts_at, m.c.id)
        )
    ).all()
    return await _details(uow, list(rows))


async def meetings_by_sources(uow: UnitOfWork, source_item_ids: list[UUID]) -> list[MeetingDetail]:
    m = meetings_table
    if not source_item_ids:
        return []
    rows = (
        await uow.session.execute(
            select(*_DETAIL_COLS)
            .where(
                m.c.user_id == uow.user_id, m.c.source_item_id.in_(source_item_ids), m.c.deleted_at.is_(None)
            )
            .order_by(m.c.starts_at, m.c.id)
        )
    ).all()
    return await _details(uow, list(rows))


async def meeting_details_between(
    uow: UnitOfWork,
    start: datetime.datetime,
    end: datetime.datetime,
    *,
    person_ids: list[UUID] | None = None,
    include_cancelled: bool = False,
    limit: int = 50,
) -> list[MeetingDetail]:
    """Meetings overlapping [start, end), optionally only those with one of ``person_ids``."""
    m, mp = meetings_table, meeting_participants_table
    stmt = select(*_DETAIL_COLS).where(
        m.c.user_id == uow.user_id, m.c.starts_at < end, m.c.ends_at > start, m.c.deleted_at.is_(None)
    )
    if not include_cancelled:
        stmt = stmt.where(m.c.status != "cancelled")
    if person_ids:
        stmt = stmt.where(
            m.c.id.in_(
                select(mp.c.meeting_id).where(mp.c.user_id == uow.user_id, mp.c.person_id.in_(person_ids))
            )
        )
    rows = (await uow.session.execute(stmt.order_by(m.c.starts_at, m.c.id).limit(limit))).all()
    return await _details(uow, list(rows))
