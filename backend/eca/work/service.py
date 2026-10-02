"""Work items, evidence and the event-first write path (BACKEND_DESIGN.md §12.2, CONTEXT_ARCHITECTURE.md §8).

Every item change goes through :func:`append_event`: in the caller's transaction it locks the
item row, inserts the ``context_events`` row (unique dedupe key, so retries and replays are
no-ops), re-folds all of the item's events, writes the projection with ``version + 1`` and
publishes ``WorkItemChanged``. User actions are authority-5 events, so the fold keeps them over
any later model event (RT-03b, RT-13).
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from eca.platform.errors import Conflict, NotFound, ValidationFailed
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork
from eca.work.events import WORK_ITEM_CHANGED, WorkItemChanged
from eca.work.fold import FOLDED_FIELDS, FoldEvent, fold
from eca.work.models import (
    context_events_table,
    evidence_table,
    item_evidence_table,
    work_items_table,
)

EVIDENCE_NAMESPACE = uuid.UUID("6f9a3c2e-0b7d-5e41-9c8a-2d4f6b1e7a03")
USER_EDITABLE = frozenset(
    {
        "title",
        "description",
        "type",
        "owner_person_id",
        "counterparty_person_id",
        "due_at",
        "due_precision",
        "due_text",
        "project_hint",
        "notes",
    }
)
LIFECYCLE = ("open", "in_progress", "done", "cancelled")


@dataclass(frozen=True)
class AppendResult:
    applied: bool
    item_id: UUID
    version: int


@dataclass(frozen=True)
class WorkItemView:
    id: UUID
    type: str
    title: str
    direction: str
    owner_person_id: UUID | None
    counterparty_person_id: UUID | None
    requester_person_id: UUID | None
    due_at: datetime.datetime | None
    due_precision: str | None
    due_text: str | None
    lifecycle_status: str
    verification_status: str
    origin: str
    commitment_strength: str | None
    statement_kind: str | None
    reported_status: str | None
    confidence: float | None
    confidence_band: str | None
    extraction_id: UUID | None
    extraction_method: str
    model: str | None
    derived_at: datetime.datetime
    user_fields: tuple[str, ...]
    has_conflict: bool
    pending_adjudication: bool
    archived: bool
    notes: str | None
    version: int
    merged_into_id: UUID | None
    evidence_source_ids: tuple[UUID, ...] = ()
    priority_score: float | None = None
    priority_reasons: tuple[dict[str, Any], ...] = ()
    has_source_gap: bool = False
    # Read-side fields (Phase 2 cards and change feed).
    reported_status_at: datetime.datetime | None = None
    reported_status_evidence_id: UUID | None = None
    project_hint: str | None = None
    last_activity_at: datetime.datetime | None = None
    stale: bool = False
    created_at: datetime.datetime | None = None
    project_id: UUID | None = None


def model_dedupe_key(extraction_id: UUID, index: int, event_type: str) -> bytes:
    return hashlib.sha256(f"model:{extraction_id}:{index}:{event_type}".encode()).digest()


def user_dedupe_key(request_key: str, event_type: str, item_id: UUID) -> bytes:
    return hashlib.sha256(f"user:{request_key}:{event_type}:{item_id}".encode()).digest()


def system_dedupe_key(*parts: object) -> bytes:
    return hashlib.sha256(("system:" + ":".join(str(p) for p in parts)).encode()).digest()


def evidence_id_for(extraction_id: UUID, index: int) -> UUID:
    """Deterministic evidence ID (BACKEND_DESIGN.md §10.1): R2 recreates the same IDs."""
    return uuid.uuid5(EVIDENCE_NAMESPACE, f"{extraction_id}:{index}")


def _jsonable(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in fields.items():
        if isinstance(v, datetime.datetime):
            out[k] = v.isoformat()
        elif isinstance(v, UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return out


_UUID_FIELDS = frozenset(
    {
        "owner_person_id",
        "counterparty_person_id",
        "requester_person_id",
        "reported_status_evidence_id",
        "project_id",
    }
)
_TS_FIELDS = frozenset({"due_at", "reported_status_at"})


def _typed(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _UUID_FIELDS:
        return UUID(str(value))
    if name in _TS_FIELDS:
        return datetime.datetime.fromisoformat(value) if isinstance(value, str) else value
    return value


async def add_evidence(
    uow: UnitOfWork,
    *,
    evidence_id: UUID,
    source_item_id: UUID,
    extraction_id: UUID | None,
    index: int | None,
    quote: str,
    char_start: int | None,
    occurred_at: datetime.datetime,
) -> UUID:
    await uow.session.execute(
        insert(evidence_table)
        .values(
            id=evidence_id,
            user_id=uow.user_id,
            source_item_id=source_item_id,
            extraction_id=extraction_id,
            candidate_index=index,
            quote=quote,
            char_start=char_start,
            char_end=None if char_start is None else char_start + len(quote),
            occurred_at=occurred_at,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return evidence_id


async def link_evidence(
    uow: UnitOfWork, item_type: str, item_id: UUID, evidence_id: UUID, relation: str
) -> None:
    await uow.session.execute(
        insert(item_evidence_table)
        .values(
            item_type=item_type,
            item_id=item_id,
            evidence_id=evidence_id,
            relation=relation,
            user_id=uow.user_id,
        )
        .on_conflict_do_nothing()
    )


async def _lock(uow: UnitOfWork, item_id: UUID) -> Any:
    t = work_items_table
    row = (
        await uow.session.execute(
            select(t.c.id, t.c.merged_into_id, t.c.version).where(t.c.id == item_id).with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise NotFound(f"work item {item_id} not found")
    if row.merged_into_id is not None:
        return await _lock(uow, row.merged_into_id)
    return row


async def _events(uow: UnitOfWork, item_id: UUID) -> list[FoldEvent]:
    c = context_events_table
    rows = await uow.session.execute(
        select(
            c.c.id,
            c.c.event_type,
            c.c.actor,
            c.c.authority,
            c.c.occurred_at,
            c.c.recorded_at,
            c.c.payload,
            c.c.evidence_id,
        ).where(c.c.entity_type == "work_item", c.c.entity_id == item_id)
    )
    return [
        FoldEvent(
            str(r.id),
            r.event_type,
            r.actor,
            r.authority,
            r.occurred_at,
            r.recorded_at,
            r.payload or {},
            str(r.evidence_id) if r.evidence_id else None,
        )
        for r in rows
    ]


async def refold(uow: UnitOfWork, item_id: UUID, *, bump: bool = True) -> int:
    """Recompute the projection from events (row must be locked). Returns the new version."""
    t = work_items_table
    result = fold(await _events(uow, item_id))
    values: dict[str, Any] = {name: _typed(name, result.fields.get(name)) for name in FOLDED_FIELDS}
    for required, default in (("lifecycle_status", "open"), ("verification_status", "suggested")):
        values[required] = values[required] or default
    for required in ("type", "title", "direction"):
        if values[required] is None:
            values.pop(required)
    values.update(
        user_fields=result.user_fields,
        has_conflict=result.has_conflict,
        state=result.state(),
        last_activity_at=result.last_activity_at,
        first_evidence_at=result.first_evidence_at,
    )
    if bump:
        values["version"] = t.c.version + 1
    row = (
        await uow.session.execute(update(t).where(t.c.id == item_id).values(**values).returning(t.c.version))
    ).one()
    return int(row.version)


async def append_event(
    uow: UnitOfWork,
    *,
    item_id: UUID,
    event_type: str,
    actor: str,
    authority: int,
    materiality: int,
    occurred_at: datetime.datetime,
    dedupe_key: bytes,
    payload: dict[str, Any] | None = None,
    evidence_id: UUID | None = None,
    extraction_id: UUID | None = None,
) -> AppendResult:
    if actor == "user" and authority != 5:
        raise ValueError("user events carry authority 5")
    row = await _lock(uow, item_id)
    target = row.id
    inserted = (
        await uow.session.execute(
            insert(context_events_table)
            .values(
                id=uuid7(),
                user_id=uow.user_id,
                entity_type="work_item",
                entity_id=target,
                event_type=event_type,
                payload=payload or {},
                evidence_id=evidence_id,
                actor=actor,
                authority=authority,
                materiality=materiality,
                extraction_id=extraction_id,
                dedupe_key=dedupe_key,
                occurred_at=occurred_at,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "dedupe_key"])
            .returning(context_events_table.c.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return AppendResult(False, target, int(row.version))
    before = await _conflict_count(uow, target)
    version = await refold(uow, target)
    after = await _conflict_count(uow, target)
    if after > before:
        await uow.session.execute(
            insert(context_events_table)
            .values(
                id=uuid7(),
                user_id=uow.user_id,
                entity_type="work_item",
                entity_id=target,
                event_type="conflict_detected",
                payload={"after_event": str(inserted)},
                actor="system",
                authority=1,
                materiality=3,
                dedupe_key=system_dedupe_key("conflict", target, inserted),
                occurred_at=occurred_at,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "dedupe_key"])
        )
    await publish(
        uow,
        NewEvent(
            WORK_ITEM_CHANGED,
            "work_item",
            target,
            WorkItemChanged(work_item_id=target, version=version, event_type=event_type),
        ),
    )
    return AppendResult(True, target, version)


async def _conflict_count(uow: UnitOfWork, item_id: UUID) -> int:
    t = work_items_table
    state = (await uow.session.execute(select(t.c.state).where(t.c.id == item_id))).scalar_one()
    return len((state or {}).get("conflicts", []))


async def create_item(
    uow: UnitOfWork,
    *,
    fields: dict[str, Any],
    origin: str,
    extraction_id: UUID | None,
    method: str,
    model: str | None,
    derived_at: datetime.datetime,
    actor: str,
    authority: int,
    materiality: int,
    occurred_at: datetime.datetime,
    dedupe_key: bytes,
    evidence_id: UUID | None = None,
    pending_adjudication: bool = False,
    by_person: UUID | None = None,
) -> UUID:
    """Insert the item row and its ``created`` event (the fold sets every field from it)."""
    if origin == "ai" and (evidence_id is None or (extraction_id is None and method == "llm")):
        raise ValidationFailed("AI-derived items need evidence and an extraction (provenance, §17.1)")
    item_id = uuid7()
    await uow.session.execute(
        insert(work_items_table).values(
            id=item_id,
            user_id=uow.user_id,
            type=fields["type"],
            title=fields["title"],
            direction=fields["direction"],
            lifecycle_status="open",
            verification_status="user_created" if origin == "user" else "suggested",
            origin=origin,
            extraction_id=extraction_id,
            extraction_method=method,
            model=model,
            derived_at=derived_at,
            user_fields=[],
            pending_adjudication=pending_adjudication,
            has_conflict=False,
            has_source_gap=False,
            stale=False,
            archived=False,
            state={},
            version=0,
        )
    )
    initial = dict(fields)
    if origin == "user":
        initial.setdefault("verification_status", "user_created")
        initial.setdefault("lifecycle_status", "open")
    await append_event(
        uow,
        item_id=item_id,
        event_type="created",
        actor=actor,
        authority=authority,
        materiality=materiality,
        occurred_at=occurred_at,
        dedupe_key=dedupe_key,
        payload={"set": _jsonable(initial), "by_person": str(by_person) if by_person else None},
        evidence_id=evidence_id,
        extraction_id=extraction_id,
    )
    if evidence_id is not None:
        await link_evidence(uow, "work_item", item_id, evidence_id, "supports")
    return item_id


# --- user actions (service level; the HTTP API arrives in slice 1.7) ------------------------------


async def _user_event(
    uow: UnitOfWork,
    item_id: UUID,
    event_type: str,
    sets: dict[str, Any],
    *,
    request_key: str,
    at: datetime.datetime,
    materiality: int = 2,
) -> AppendResult:
    return await append_event(
        uow,
        item_id=item_id,
        event_type=event_type,
        actor="user",
        authority=5,
        materiality=materiality,
        occurred_at=at,
        dedupe_key=user_dedupe_key(request_key, event_type, item_id),
        payload={"set": _jsonable(sets)},
    )


async def user_edit(
    uow: UnitOfWork, item_id: UUID, changes: dict[str, Any], *, request_key: str, at: datetime.datetime
) -> AppendResult:
    unknown = set(changes) - USER_EDITABLE
    if unknown:
        raise ValidationFailed(f"not editable: {sorted(unknown)}")
    return await _user_event(uow, item_id, "user_edit", changes, request_key=request_key, at=at)


async def confirm(uow: UnitOfWork, item_id: UUID, *, request_key: str, at: datetime.datetime) -> AppendResult:
    return await _user_event(
        uow, item_id, "user_confirmed", {"verification_status": "confirmed"}, request_key=request_key, at=at
    )


async def reject(uow: UnitOfWork, item_id: UUID, *, request_key: str, at: datetime.datetime) -> AppendResult:
    return await _user_event(
        uow, item_id, "user_rejected", {"verification_status": "rejected"}, request_key=request_key, at=at
    )


async def set_lifecycle(
    uow: UnitOfWork, item_id: UUID, status: str, *, request_key: str, at: datetime.datetime
) -> AppendResult:
    if status not in LIFECYCLE:
        raise ValidationFailed(f"unknown lifecycle status {status!r}")
    item = await get_item(uow, item_id)
    if item.lifecycle_status == "cancelled" and status == "done":
        raise Conflict("a cancelled item cannot be completed")
    return await _user_event(
        uow,
        item_id,
        "lifecycle_changed",
        {"lifecycle_status": status},
        request_key=request_key,
        at=at,
        materiality=3,
    )


async def add_note(
    uow: UnitOfWork, item_id: UUID, note: str, *, request_key: str, at: datetime.datetime
) -> AppendResult:
    return await _user_event(
        uow, item_id, "user_edit", {"notes": note}, request_key=request_key, at=at, materiality=1
    )


async def assign_project(
    uow: UnitOfWork, item_id: UUID, project_id: UUID | None, *, request_key: str, at: datetime.datetime
) -> AppendResult:
    """User assigns (or clears) an item's project (authority 5; BACKEND_DESIGN.md §9.9)."""
    return await _user_event(
        uow,
        item_id,
        "user_project",
        {"project_id": project_id},
        request_key=request_key,
        at=at,
        materiality=1,
    )


async def record_entity_event(
    uow: UnitOfWork,
    *,
    entity_type: str,
    entity_id: UUID,
    event_type: str,
    actor: str,
    authority: int,
    materiality: int,
    occurred_at: datetime.datetime,
    dedupe_key: bytes,
    payload: dict[str, Any] | None = None,
) -> bool:
    """A ``context_events`` row for an entity without a fold (projects, decisions): the change
    feed and timelines see it; ``work`` stays the single writer (BACKEND_DESIGN.md §6.2)."""
    if entity_type == "work_item":
        raise ValidationFailed("work item events go through append_event")
    if actor == "user" and authority != 5:
        raise ValueError("user events carry authority 5")
    inserted = (
        await uow.session.execute(
            insert(context_events_table)
            .values(
                id=uuid7(),
                user_id=uow.user_id,
                entity_type=entity_type,
                entity_id=entity_id,
                event_type=event_type,
                payload=payload or {},
                actor=actor,
                authority=authority,
                materiality=materiality,
                dedupe_key=dedupe_key,
                occurred_at=occurred_at,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "dedupe_key"])
            .returning(context_events_table.c.id)
        )
    ).scalar_one_or_none()
    return inserted is not None


async def create_user_item(
    uow: UnitOfWork,
    *,
    title: str,
    type_: str,
    direction: str,
    owner_person_id: UUID | None,
    due_at: datetime.datetime | None,
    request_key: str,
    at: datetime.datetime,
) -> UUID:
    return await create_item(
        uow,
        fields={
            "type": type_,
            "title": title,
            "direction": direction,
            "owner_person_id": owner_person_id,
            "due_at": due_at,
            "due_precision": "day" if due_at else None,
        },
        origin="user",
        extraction_id=None,
        method="user",
        model=None,
        derived_at=at,
        actor="user",
        authority=5,
        materiality=2,
        occurred_at=at,
        dedupe_key=user_dedupe_key(request_key, "created", uuid.UUID(int=0)),
    )


# --- queries ----------------------------------------------------------------------------------


def _view(row: Any, sources: tuple[UUID, ...] = ()) -> WorkItemView:
    return WorkItemView(
        id=row.id,
        type=row.type,
        title=row.title,
        direction=row.direction,
        owner_person_id=row.owner_person_id,
        counterparty_person_id=row.counterparty_person_id,
        requester_person_id=row.requester_person_id,
        due_at=row.due_at,
        due_precision=row.due_precision,
        due_text=row.due_text,
        lifecycle_status=row.lifecycle_status,
        verification_status=row.verification_status,
        origin=row.origin,
        commitment_strength=row.commitment_strength,
        statement_kind=row.statement_kind,
        reported_status=row.reported_status,
        confidence=row.confidence,
        confidence_band=row.confidence_band,
        extraction_id=row.extraction_id,
        extraction_method=row.extraction_method,
        model=row.model,
        derived_at=row.derived_at,
        user_fields=tuple(row.user_fields or ()),
        has_conflict=row.has_conflict,
        pending_adjudication=row.pending_adjudication,
        archived=row.archived,
        notes=row.notes,
        version=row.version,
        merged_into_id=row.merged_into_id,
        evidence_source_ids=sources,
        priority_score=getattr(row, "priority_score", None),
        priority_reasons=tuple(getattr(row, "priority_reasons", None) or ()),
        has_source_gap=bool(getattr(row, "has_source_gap", False)),
        reported_status_at=getattr(row, "reported_status_at", None),
        reported_status_evidence_id=getattr(row, "reported_status_evidence_id", None),
        project_hint=getattr(row, "project_hint", None),
        last_activity_at=getattr(row, "last_activity_at", None),
        stale=bool(getattr(row, "stale", False)),
        created_at=getattr(row, "created_at", None),
        project_id=getattr(row, "project_id", None),
    )


async def evidence_sources(uow: UnitOfWork, item_id: UUID) -> tuple[UUID, ...]:
    ie, ev = item_evidence_table, evidence_table
    rows = await uow.session.execute(
        select(ev.c.source_item_id)
        .join(ie, ie.c.evidence_id == ev.c.id)
        .where(ie.c.item_type == "work_item", ie.c.item_id == item_id, ie.c.relation != "superseded")
        .distinct()
    )
    return tuple(sorted(r.source_item_id for r in rows))


async def get_item(uow: UnitOfWork, item_id: UUID) -> WorkItemView:
    t = work_items_table
    row = (await uow.session.execute(select(t).where(t.c.id == item_id))).one_or_none()
    if row is None:
        raise NotFound(f"work item {item_id} not found")
    return _view(row, await evidence_sources(uow, item_id))


async def list_items(
    uow: UnitOfWork,
    *,
    direction: str | None = None,
    include_closed: bool = True,
    include_rejected: bool = False,
) -> list[WorkItemView]:
    t = work_items_table
    stmt = select(t).where(t.c.merged_into_id.is_(None), t.c.deleted_at.is_(None))
    if direction is not None:
        stmt = stmt.where(t.c.direction == direction)
    if not include_closed:
        stmt = stmt.where(t.c.lifecycle_status.in_(["open", "in_progress"]))
    if not include_rejected:
        stmt = stmt.where(t.c.verification_status != "rejected")
    rows = (await uow.session.execute(stmt.order_by(t.c.created_at, t.c.id))).all()
    return [_view(r, await evidence_sources(uow, r.id)) for r in rows]


async def timeline(uow: UnitOfWork, item_id: UUID) -> list[dict[str, Any]]:
    c = context_events_table
    rows = await uow.session.execute(
        select(
            c.c.event_type,
            c.c.actor,
            c.c.authority,
            c.c.materiality,
            c.c.occurred_at,
            c.c.recorded_at,
            c.c.payload,
            c.c.evidence_id,
            c.c.extraction_id,
        )
        .where(c.c.entity_type == "work_item", c.c.entity_id == item_id)
        .order_by(c.c.occurred_at, c.c.recorded_at, c.c.id)
    )
    return [dict(r._mapping) for r in rows]


async def count_items(uow: UnitOfWork) -> int:
    t = work_items_table
    return int((await uow.session.execute(select(func.count()).select_from(t))).scalar_one())
