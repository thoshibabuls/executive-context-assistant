"""User commands on work items and decisions for the API (slice 1.7, BACKEND_DESIGN.md §12.3, §16).

Every correction is a user event (authority 5) and a ``feedback_events`` row in the same
transaction. Concurrency (§12.3): ``If-Match`` with the item version → ``412`` when stale;
``base_version`` → field-level check: only a change to a field that another event set since that
version is a ``409`` (with the fields); unrelated concurrent changes merge.
"""

from __future__ import annotations

import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from eca.platform.errors import Conflict, NotFound, PreconditionFailed, ValidationFailed
from eca.platform.feedback import record_feedback
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork
from eca.work.models import context_events_table, decisions_table, item_evidence_table, work_items_table
from eca.work.queries import DecisionView, decision_view
from eca.work.service import (
    USER_EDITABLE,
    AppendResult,
    append_event,
    confirm,
    create_user_item,
    get_item,
    reject,
    set_lifecycle,
    system_dedupe_key,
    user_dedupe_key,
    user_edit,
)

MERGE_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended('merge:' || :user_id, 0))")
LIFECYCLE_COMMANDS = {"complete": "done", "reopen": "open", "cancel": "cancelled", "start": "in_progress"}


def _snapshot(item: Any, fields: set[str] | frozenset[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in sorted(fields):
        v = getattr(item, f, None)
        out[f] = v.isoformat() if isinstance(v, datetime.datetime) else (str(v) if isinstance(v, UUID) else v)
    return out


async def _fields_changed_since(uow: UnitOfWork, item_id: UUID, current: int, base: int) -> set[str]:
    """Fields set by the events after ``base`` (the last ``current - base`` events)."""
    c = context_events_table
    n = current - base
    if n <= 0:
        return set()
    rows = await uow.session.execute(
        select(c.c.payload)
        .where(c.c.entity_type == "work_item", c.c.entity_id == item_id)
        .order_by(c.c.recorded_at.desc(), c.c.id.desc())
        .limit(n)
    )
    changed: set[str] = set()
    for r in rows:
        changed |= set((r.payload or {}).get("set", {}))
    return changed


async def edit_item(
    uow: UnitOfWork,
    item_id: UUID,
    changes: dict[str, Any],
    *,
    request_key: str,
    at: datetime.datetime,
    if_match: int | None = None,
    base_version: int | None = None,
) -> AppendResult:
    if not changes:
        raise ValidationFailed("no fields to change")
    unknown = set(changes) - USER_EDITABLE
    if unknown:
        raise ValidationFailed(f"not editable: {sorted(unknown)}")
    item = await get_item(uow, item_id)
    if if_match is not None and if_match != item.version:
        raise PreconditionFailed(
            "the item changed since you loaded it", details={"current_version": item.version}
        )
    if base_version is not None and base_version != item.version:
        clashing = await _fields_changed_since(uow, item_id, item.version, base_version) & set(changes)
        if clashing:
            raise Conflict(
                "fields changed since your version", fields=sorted(clashing), current_version=item.version
            )
    before = _snapshot(item, set(changes))
    result = await user_edit(uow, item_id, changes, request_key=request_key, at=at)
    if result.applied:
        await record_feedback(
            uow, target_type="work_item", target_id=item_id, action="edit", before=before, after=changes
        )
    return result


async def verify_item(
    uow: UnitOfWork, item_id: UUID, verdict: str, *, request_key: str, at: datetime.datetime
) -> AppendResult:
    item = await get_item(uow, item_id)
    fn = {"confirm": confirm, "reject": reject}.get(verdict)
    if fn is None:
        raise ValidationFailed(f"unknown verdict {verdict!r}")
    result = await fn(uow, item_id, request_key=request_key, at=at)
    if result.applied:
        await record_feedback(
            uow,
            target_type="work_item",
            target_id=item_id,
            action=verdict,
            before={"verification_status": item.verification_status, "origin": item.origin},
            after=None,
        )
    return result


async def lifecycle_command(
    uow: UnitOfWork, item_id: UUID, command: str, *, request_key: str, at: datetime.datetime
) -> AppendResult:
    status = LIFECYCLE_COMMANDS.get(command)
    if status is None:
        raise ValidationFailed(f"unknown command {command!r}")
    item = await get_item(uow, item_id)
    result = await set_lifecycle(uow, item_id, status, request_key=request_key, at=at)
    if result.applied:
        await record_feedback(
            uow,
            target_type="work_item",
            target_id=item_id,
            action=command,
            before={"lifecycle_status": item.lifecycle_status},
            after={"lifecycle_status": status},
        )
    return result


async def create_item_for_user(
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
    if not title.strip():
        raise ValidationFailed("title is required")
    return await create_user_item(
        uow,
        title=title.strip(),
        type_=type_,
        direction=direction,
        owner_person_id=owner_person_id,
        due_at=due_at,
        request_key=request_key,
        at=at,
    )


async def delete_user_item(uow: UnitOfWork, item_id: UUID, *, at: datetime.datetime) -> None:
    """Logical delete of a user-created item (restorable for 30 days); AI items use ``reject``."""
    t = work_items_table
    row = (
        await uow.session.execute(
            select(t.c.origin, t.c.deleted_at).where(t.c.id == item_id).with_for_update()
        )
    ).one_or_none()
    if row is None or row.deleted_at is not None:
        raise NotFound(f"work item {item_id} not found")
    if row.origin != "user":
        raise Conflict("AI-suggested items are rejected, not deleted", details={"use": "reject"})
    await uow.session.execute(
        update(t).where(t.c.id == item_id).values(deleted_at=at, version=t.c.version + 1)
    )
    await record_feedback(uow, target_type="work_item", target_id=item_id, action="delete")


async def merge_items(
    uow: UnitOfWork, source_id: UUID, into_id: UUID, *, request_key: str, at: datetime.datetime
) -> AppendResult:
    """User merge (§12.5): the source redirects to the target; its evidence links move over."""
    if source_id == into_id:
        raise ValidationFailed("an item cannot be merged into itself")
    await uow.session.execute(MERGE_LOCK_SQL, {"user_id": str(uow.user_id)})
    t, ie, c = work_items_table, item_evidence_table, context_events_table
    rows = {
        r.id: r
        for r in await uow.session.execute(
            select(t.c.id, t.c.merged_into_id, t.c.deleted_at)
            .where(t.c.id.in_([source_id, into_id]))
            .with_for_update()
        )
    }
    for item_id in (source_id, into_id):
        if item_id not in rows or rows[item_id].deleted_at is not None:
            raise NotFound(f"work item {item_id} not found")
    if rows[source_id].merged_into_id is not None or rows[into_id].merged_into_id is not None:
        raise Conflict("one of the items was already merged")
    links = await uow.session.execute(
        select(ie.c.evidence_id, ie.c.relation).where(
            ie.c.item_type == "work_item", ie.c.item_id == source_id
        )
    )
    for link in links:
        await uow.session.execute(
            pg_insert(ie)
            .values(
                item_type="work_item",
                item_id=into_id,
                evidence_id=link.evidence_id,
                relation=link.relation,
                user_id=uow.user_id,
            )
            .on_conflict_do_nothing()
        )
    await uow.session.execute(
        pg_insert(c)
        .values(
            id=uuid7(),
            user_id=uow.user_id,
            entity_type="work_item",
            entity_id=source_id,
            event_type="merged_into",
            payload={"into_id": str(into_id)},
            actor="user",
            authority=5,
            materiality=2,
            dedupe_key=system_dedupe_key("merged_into", source_id, into_id),
            occurred_at=at,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "dedupe_key"])
    )
    await uow.session.execute(
        update(t).where(t.c.id == source_id).values(merged_into_id=into_id, version=t.c.version + 1)
    )
    result = await append_event(
        uow,
        item_id=into_id,
        event_type="merged",
        actor="user",
        authority=5,
        materiality=2,
        occurred_at=at,
        dedupe_key=user_dedupe_key(request_key, "merged", into_id),
        payload={"merged_from": str(source_id)},
    )
    await record_feedback(
        uow, target_type="work_item", target_id=source_id, action="merge", after={"into_id": str(into_id)}
    )
    return result


# --- decisions ------------------------------------------------------------------------------

DECISION_EDITABLE = frozenset({"statement", "rationale", "decided_at", "project_hint", "notes", "kind"})


async def _locked_decision(uow: UnitOfWork, decision_id: UUID) -> Any:
    d = decisions_table
    row = (
        await uow.session.execute(
            select(d).where(d.c.id == decision_id, d.c.deleted_at.is_(None)).with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise NotFound(f"decision {decision_id} not found")
    return row


async def edit_decision(
    uow: UnitOfWork, decision_id: UUID, changes: dict[str, Any], *, if_match: int | None
) -> DecisionView:
    unknown = set(changes) - DECISION_EDITABLE
    if unknown or not changes:
        raise ValidationFailed(f"not editable: {sorted(unknown)}" if unknown else "no fields to change")
    if "kind" in changes and changes["kind"] not in ("decision", "open_question"):
        raise ValidationFailed("kind must be decision or open_question")
    row = await _locked_decision(uow, decision_id)
    if if_match is not None and if_match != row.version:
        raise PreconditionFailed(
            "the decision changed since you loaded it", details={"current_version": row.version}
        )
    d = decisions_table
    user_fields = sorted(set(row.user_fields or ()) | set(changes))
    values = dict(changes)
    if isinstance(values.get("decided_at"), str):
        values["decided_at"] = datetime.datetime.fromisoformat(values["decided_at"])
    new = (
        await uow.session.execute(
            update(d)
            .where(d.c.id == decision_id)
            .values(**values, user_fields=user_fields, version=d.c.version + 1)
            .returning(*d.c)
        )
    ).one()
    await record_feedback(
        uow,
        target_type="decision",
        target_id=decision_id,
        action="edit",
        before=_snapshot(row, set(changes)),
        after={k: (v.isoformat() if isinstance(v, datetime.datetime) else v) for k, v in values.items()},
    )
    return decision_view(new)


async def decision_command(
    uow: UnitOfWork, decision_id: UUID, command: str, *, at: datetime.datetime
) -> DecisionView:
    """``confirm`` / ``reject`` (verification) and ``resolve`` (an open question answered)."""
    row = await _locked_decision(uow, decision_id)
    d = decisions_table
    if command == "confirm":
        values: dict[str, Any] = {"verification_status": "confirmed"}
    elif command == "reject":
        values = {"verification_status": "rejected"}
    elif command == "resolve":
        if row.kind != "open_question":
            raise Conflict("only open questions are resolved")
        values = {"kind": "decision", "decided_at": row.decided_at or at, "verification_status": "confirmed"}
    else:
        raise ValidationFailed(f"unknown command {command!r}")
    new = (
        await uow.session.execute(
            update(d).where(d.c.id == decision_id).values(**values, version=d.c.version + 1).returning(*d.c)
        )
    ).one()
    await record_feedback(
        uow,
        target_type="decision",
        target_id=decision_id,
        action=command,
        before={"verification_status": row.verification_status, "kind": row.kind},
    )
    return decision_view(new)
