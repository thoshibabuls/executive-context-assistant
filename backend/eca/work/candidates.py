"""Candidate context for extraction (CONTEXT_ARCHITECTURE.md §12.1) and deterministic matching
at apply time (TECHNICAL_DESIGN.md §13.6).

Batch A has no item embeddings (``dedupe_embedding`` stays NULL, BACKEND_DESIGN.md §17), so text
similarity uses the documented degraded mode: normalized token overlap and sequence similarity
instead of cosine (AI_PIPELINE.md §14 "Embeddings down → trigram matching").
"""

from __future__ import annotations

import datetime
import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from eca.communication import MessageView, conversation_source_items
from eca.intelligence import Candidate
from eca.people import get_persons
from eca.platform.uow import UnitOfWork
from eca.work.dates import compatible_due
from eca.work.models import evidence_table, item_evidence_table, work_items_table

MAX_CANDIDATES = 8
MERGE_THRESHOLD = 0.72  # stands in for cosine ≥ 0.88 until embeddings exist
AMBIGUOUS_THRESHOLD = 0.55  # stands in for the 0.80-0.88 "possible duplicate" band
_STOP = frozenset(
    "a an the to of for on in by and or with you your i we it this that me my our will be".split()  # noqa: SIM905
)
_WORD = re.compile(r"[a-z0-9]+")
FAMILY = {
    "commitment": "work",
    "request": "work",
    "task": "work",
    "follow_up": "work",
    "deadline": "deadline",
}


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(set(ta) & set(tb)) / len(set(ta) | set(tb))
    seq = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    return max(jaccard, seq)


@dataclass(frozen=True)
class OpenItem:
    id: UUID
    type: str
    title: str
    owner_person_id: UUID | None
    counterparty_person_id: UUID | None
    due_at: datetime.datetime | None
    due_text: str | None
    lifecycle_status: str
    verification_status: str
    reported_status: str | None
    version: int
    last_activity_at: datetime.datetime | None


_ITEM_COLS = (
    work_items_table.c.id,
    work_items_table.c.type,
    work_items_table.c.title,
    work_items_table.c.owner_person_id,
    work_items_table.c.counterparty_person_id,
    work_items_table.c.due_at,
    work_items_table.c.due_text,
    work_items_table.c.lifecycle_status,
    work_items_table.c.verification_status,
    work_items_table.c.reported_status,
    work_items_table.c.version,
    work_items_table.c.last_activity_at,
)


async def matchable_items(uow: UnitOfWork, *, now: datetime.datetime) -> list[OpenItem]:
    """Open items, items closed in the last 30 days and items rejected in the last 90 (§13.6)."""
    t = work_items_table
    rows = await uow.session.execute(
        select(*_ITEM_COLS)
        .where(t.c.merged_into_id.is_(None), t.c.deleted_at.is_(None))
        .order_by(t.c.created_at, t.c.id)
    )
    out = []
    for r in rows:
        age = now - (r.last_activity_at or now)
        if r.verification_status == "rejected" and age > datetime.timedelta(days=90):
            continue
        if r.lifecycle_status in ("done", "cancelled") and age > datetime.timedelta(days=30):
            continue
        out.append(OpenItem(**r._mapping))
    return out


async def _thread_item_ids(uow: UnitOfWork, conversation_id: UUID) -> list[UUID]:
    sources = await conversation_source_items(uow, conversation_id)
    if not sources:
        return []
    ie, ev = item_evidence_table, evidence_table
    rows = await uow.session.execute(
        select(ie.c.item_id)
        .join(ev, ev.c.id == ie.c.evidence_id)
        .where(ie.c.item_type == "work_item", ev.c.source_item_id.in_(sources))
        .distinct()
    )
    return sorted(r.item_id for r in rows)


async def build_candidates(uow: UnitOfWork, view: MessageView, *, self_id: UUID) -> list[Candidate]:
    items = {i.id: i for i in await matchable_items(uow, now=view.sent_at)}
    thread_ids = [i for i in await _thread_item_ids(uow, view.conversation_id) if i in items]
    participants = {view.sender.id, *(p.id for p in view.to), *(p.id for p in view.cc)} - {self_id}
    window = view.sent_at - datetime.timedelta(days=60)
    by_people = sorted(
        (
            i
            for i in items.values()
            if i.id not in thread_ids
            and ({i.owner_person_id, i.counterparty_person_id} & participants)
            and (i.last_activity_at is None or i.last_activity_at >= window)
        ),
        key=lambda i: (-(i.last_activity_at or view.sent_at).timestamp(), str(i.id)),
    )
    ordered = [items[i] for i in thread_ids] + by_people
    open_first = [i for i in ordered if i.verification_status != "rejected"]
    rejected = [i for i in ordered if i.verification_status == "rejected"]
    chosen = (open_first + rejected)[:MAX_CANDIDATES]
    persons = await get_persons(
        uow, [p for i in chosen for p in (i.owner_person_id, i.counterparty_person_id) if p]
    )
    out = []
    for n, item in enumerate(chosen, start=1):
        owner = persons.get(item.owner_person_id) if item.owner_person_id else None
        owner_name = (
            "the user"
            if owner and owner.is_self
            else (owner.display_name or owner.primary_email if owner else "unknown")
        )
        due = item.due_text or (item.due_at.date().isoformat() if item.due_at else "none")
        status = item.reported_status or item.lifecycle_status
        out.append(
            Candidate(
                code=f"C{n}",
                entity_type="work_item",
                entity_id=item.id,
                version=item.version,
                summary=f"{item.type}: {item.title}; owner {owner_name}; due {due}; status {status}",
                rejected=item.verification_status == "rejected",
            )
        )
    return out


@dataclass(frozen=True)
class Match:
    item: OpenItem
    score: float


def match_items(
    items: Sequence[OpenItem],
    *,
    type_: str,
    title: str,
    owner_id: UUID | None,
    counterparty_id: UUID | None,
    due_at: datetime.datetime | None,
    exclude: frozenset[UUID] = frozenset(),
) -> tuple[Match | None, list[Match]]:
    """(merge target, ambiguous matches) under §13.6's rules (degraded similarity)."""
    family = FAMILY.get(type_, type_)
    scored = []
    for item in items:
        if item.id in exclude or FAMILY.get(item.type, item.type) != family:
            continue
        if owner_id is None or item.owner_person_id != owner_id:
            continue
        if counterparty_id and item.counterparty_person_id and counterparty_id != item.counterparty_person_id:
            continue
        if not compatible_due(due_at, item.due_at):
            continue
        score = similarity(title, item.title)
        if score >= AMBIGUOUS_THRESHOLD:
            scored.append(Match(item, score))
    scored.sort(key=lambda m: (-m.score, str(m.item.id)))
    if (
        scored
        and scored[0].score >= MERGE_THRESHOLD
        and (len(scored) == 1 or scored[1].score < MERGE_THRESHOLD)
    ):
        return scored[0], []
    return None, scored
