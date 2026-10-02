"""Hybrid discovery: vector + full text fused with RRF in one statement (CONTEXT_ARCHITECTURE.md
§9.9, §9.10). Discovery only: it finds anchors and supporting text; state comes from relational
recipes (§6.1 rule).

Scope is applied inside both ranked lists, before fusion and ranking (§9.7): an explicit
``user_id`` predicate, the visible-source subquery of ``ingestion`` (not deleted, not trashed,
calendar scope), the session's allowed sources, and the plan's time, person and conversation
filters. ``hnsw.iterative_scan = relaxed_order`` keeps per-user filters from starving the
vector list.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Float,
    and_,
    cast,
    false,
    func,
    literal,
    literal_column,
    null,
    select,
    text,
    true,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from eca import ingestion
from eca.platform.uow import UnitOfWork
from eca.retrieval.models import HalfVec, chunks_table

LIST_SIZE = 40  # top K of each ranked list (§9.9)
RRF_K = 60
DISCOVERY_K = 20
PER_CONVERSATION_CAP = 3  # diversity applies to discovery only (A14)
MIN_COSINE = 0.60  # match floor for the abstention pre-check (§9.10)
MAX_TERMS = 12
HNSW_SETTINGS = (
    "SET LOCAL hnsw.iterative_scan = relaxed_order",
    "SET LOCAL hnsw.ef_search = 100",
)
_TOKEN = re.compile(r"[A-Za-z0-9]+")


def ts_terms(text_: str | None) -> str:
    """OR of alphanumeric terms (≥ 2 characters, at most 12) for ``to_tsquery`` (§9.10).

    The output contains only ``[A-Za-z0-9]`` and ``" | "``, so it is always a valid tsquery.
    """
    seen: list[str] = []
    for token in _TOKEN.findall(text_ or ""):
        t = token.lower()
        if len(t) >= 2 and t not in seen:
            seen.append(t)
        if len(seen) == MAX_TERMS:
            break
    return " | ".join(seen)


@dataclass(frozen=True)
class SearchFilters:
    since: datetime.datetime | None = None
    until: datetime.datetime | None = None
    person_ids: tuple[UUID, ...] = ()
    conversation_ids: tuple[UUID, ...] = ()
    kinds: tuple[str, ...] = ()
    allowed_sources: tuple[UUID, ...] | None = None  # session scope (meeting) or project scope
    exclude_calendar_connections: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class ChunkHit:
    id: UUID
    source_item_id: UUID
    kind: str
    title: str | None
    text: str
    occurred_at: datetime.datetime
    conversation_id: UUID | None
    meeting_id: UUID | None
    person_ids: tuple[UUID, ...]
    rrf: float
    cosine: float | None
    in_fts: bool

    @property
    def is_match(self) -> bool:
        """Above the floor for the abstention pre-check (§9.10)."""
        return self.in_fts or (self.cosine is not None and self.cosine >= MIN_COSINE)


def _filters(user_id: UUID, f: SearchFilters) -> list[ColumnElement[bool]]:
    c = chunks_table.c
    visible = ingestion.visible_source_ids(
        user_id, exclude_calendar_connections=f.exclude_calendar_connections, only=f.allowed_sources
    )
    conds: list[ColumnElement[bool]] = [c.user_id == user_id, c.source_item_id.in_(visible)]
    if f.since is not None:
        conds.append(c.occurred_at >= f.since)
    if f.until is not None:
        conds.append(c.occurred_at < f.until)
    if f.person_ids:
        conds.append(c.person_ids.overlap(cast(list(f.person_ids), ARRAY(PG_UUID(as_uuid=True)))))
    if f.conversation_ids:
        conds.append(c.conversation_id.in_(list(f.conversation_ids)))
    if f.kinds:
        conds.append(c.kind.in_(list(f.kinds)))
    return conds


async def hybrid_search(
    uow: UnitOfWork,
    *,
    terms: str,
    query_vector: str | None,
    embedding_model: str | None,
    filters: SearchFilters,
    limit: int = DISCOVERY_K,
) -> list[ChunkHit]:
    """Top chunks by RRF (``Σ 1/(60 + rank)``) over the vector and FTS lists, best first."""
    if uow.user_id is None:
        return []
    c = chunks_table.c
    conds = _filters(uow.user_id, filters)
    branches: list[Any] = []
    if query_vector is not None and embedding_model is not None:
        for stmt in HNSW_SETTINGS:
            await uow.session.execute(text(stmt))
        distance = c.embedding.op("<=>", return_type=Float())(cast(literal(query_vector), HalfVec()))
        inner = (
            select(c.id.label("id"), distance.label("dist"))
            .where(*conds, c.embedding.is_not(None), c.embedding_model == embedding_model)
            .order_by(distance)
            .limit(LIST_SIZE)
            .subquery()
        )
        branches.append(
            select(
                inner.c.id,
                func.row_number().over(order_by=(inner.c.dist, inner.c.id)).label("r"),
                (literal(1.0) - inner.c.dist).label("cosine"),
                false().label("fts"),
            )
        )
    if terms:
        query = func.to_tsquery(literal_column("'english'::regconfig"), terms)
        rank = func.ts_rank_cd(c.tsv, query, 32)
        inner_f = (
            select(c.id.label("id"), rank.label("rank"))
            .where(*conds, c.tsv.op("@@")(query))
            .order_by(rank.desc(), c.id)
            .limit(LIST_SIZE)
            .subquery()
        )
        branches.append(
            select(
                inner_f.c.id,
                func.row_number().over(order_by=(inner_f.c.rank.desc(), inner_f.c.id)).label("r"),
                cast(null(), Float()).label("cosine"),
                true().label("fts"),
            )
        )
    if not branches:
        return []
    union = union_all(*branches).subquery() if len(branches) > 1 else branches[0].subquery()
    fused = (
        select(
            union.c.id,
            func.sum(literal(1.0) / (RRF_K + union.c.r)).label("rrf"),
            func.max(union.c.cosine).label("cosine"),
            func.bool_or(union.c.fts).label("in_fts"),
        )
        .group_by(union.c.id)
        .subquery()
    )
    rows = (
        await uow.session.execute(
            select(
                c.id,
                c.source_item_id,
                c.kind,
                c.title,
                c.text,
                c.occurred_at,
                c.conversation_id,
                c.meeting_id,
                c.person_ids,
                fused.c.rrf,
                fused.c.cosine,
                fused.c.in_fts,
            )
            .join(fused, and_(fused.c.id == c.id, c.user_id == uow.user_id))
            .order_by(fused.c.rrf.desc(), c.id)
            .limit(LIST_SIZE * 2)
        )
    ).all()
    hits = [
        ChunkHit(
            r.id,
            r.source_item_id,
            r.kind,
            r.title,
            r.text,
            r.occurred_at,
            r.conversation_id,
            r.meeting_id,
            tuple(r.person_ids or ()),
            float(r.rrf),
            None if r.cosine is None else float(r.cosine),
            bool(r.in_fts),
        )
        for r in rows
    ]
    return diversify(hits, limit=limit)


def diversify(hits: Sequence[ChunkHit], *, limit: int, cap: int = PER_CONVERSATION_CAP) -> list[ChunkHit]:
    """At most ``cap`` chunks per conversation, in rank order (discovery only, A14)."""
    out: list[ChunkHit] = []
    per: dict[UUID, int] = {}
    for hit in hits:
        if hit.conversation_id is not None:
            if per.get(hit.conversation_id, 0) >= cap:
                continue
            per[hit.conversation_id] = per.get(hit.conversation_id, 0) + 1
        out.append(hit)
        if len(out) == limit:
            break
    return out
