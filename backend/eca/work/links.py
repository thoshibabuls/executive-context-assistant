"""``entity_links`` (BACKEND_DESIGN.md §5.1, §17.5; CONTEXT_ARCHITECTURE.md §12.2, §12.7).

``work`` is the single writer. Computed links (thread continuation, written by the retrieval
index job through :func:`link_entities`) are inserted once and never overwrite an existing link,
so a user's confirmation or rejection of a link survives re-indexing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert

from eca.platform.errors import ValidationFailed
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork
from eca.work.models import entity_links_table

LINK_TYPES = frozenset({"work_item", "decision", "conversation", "meeting", "project"})
RELATIONS = frozenset({"continues", "possible_duplicate", "relates_to"})


@dataclass(frozen=True)
class EntityLink:
    from_type: str
    from_id: UUID
    to_type: str
    to_id: UUID
    relation: str
    confidence: float
    method: str
    origin: str
    verification_status: str


async def link_entities(
    uow: UnitOfWork,
    *,
    from_type: str,
    from_id: UUID,
    to_type: str,
    to_id: UUID,
    relation: str,
    confidence: float,
    method: str,
    origin: str,
    scores: dict[str, Any] | None = None,
) -> bool:
    """Insert a link if it does not exist; returns True when a row was written."""
    if from_type not in LINK_TYPES or to_type not in LINK_TYPES or relation not in RELATIONS:
        raise ValidationFailed("unknown link type or relation")
    if from_type == to_type and from_id == to_id:
        raise ValidationFailed("an entity cannot link to itself")
    t = entity_links_table
    inserted = (
        await uow.session.execute(
            insert(t)
            .values(
                id=uuid7(),
                user_id=uow.user_id,
                from_type=from_type,
                from_id=from_id,
                to_type=to_type,
                to_id=to_id,
                relation=relation,
                confidence=max(0.0, min(1.0, confidence)),
                method=method,
                origin=origin,
                verification_status="suggested",
                scores=scores or {},
            )
            .on_conflict_do_nothing(constraint="ux_entity_links")
            .returning(t.c.id)
        )
    ).first()
    return inserted is not None


async def links_of(
    uow: UnitOfWork, entity_type: str, entity_ids: Sequence[UUID], *, relation: str | None = None
) -> list[EntityLink]:
    """Links in either direction touching the given entities; rejected links are left out."""
    ids = list(entity_ids)
    if not ids:
        return []
    t = entity_links_table
    stmt = select(t).where(
        t.c.user_id == uow.user_id,
        t.c.verification_status != "rejected",
        or_(
            (t.c.from_type == entity_type) & t.c.from_id.in_(ids),
            (t.c.to_type == entity_type) & t.c.to_id.in_(ids),
        ),
    )
    if relation is not None:
        stmt = stmt.where(t.c.relation == relation)
    rows = await uow.session.execute(stmt.order_by(t.c.created_at, t.c.id))
    return [
        EntityLink(
            r.from_type,
            r.from_id,
            r.to_type,
            r.to_id,
            r.relation,
            float(r.confidence),
            r.method,
            r.origin,
            r.verification_status,
        )
        for r in rows
    ]


async def delete_links(uow: UnitOfWork, entity_type: str, entity_ids: Sequence[UUID]) -> None:
    """Links touching entities that a purge removes (no FK on polymorphic endpoints, §17.1)."""
    ids = list(entity_ids)
    if not ids:
        return
    t = entity_links_table
    await uow.session.execute(
        delete(t).where(
            t.c.user_id == uow.user_id,
            or_(
                (t.c.from_type == entity_type) & t.c.from_id.in_(ids),
                (t.c.to_type == entity_type) & t.c.to_id.in_(ids),
            ),
        )
    )


async def purge_user_links(uow: UnitOfWork) -> None:
    await uow.session.execute(delete(entity_links_table).where(entity_links_table.c.user_id == uow.user_id))
