"""``retrieval_traces``: one content-free row per packet assembly (CONTEXT_ARCHITECTURE.md §9.10).

IDs, scores, counts, coverage statuses and timings only; the question is stored as a SHA-256
hash (its text lives in ``chat_messages``, which the user can delete). Evaluation (L3, E6) reads
plan, candidates and selected IDs from here; traces are deleted after 90 days.
"""

from __future__ import annotations

import datetime
import hashlib
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, insert

from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork
from eca.retrieval.models import retrieval_traces_table
from eca.retrieval.packet import Packet

TRACE_RETENTION = datetime.timedelta(days=90)
MAX_CANDIDATES = 200


def question_hash(question: str) -> bytes:
    return hashlib.sha256(question.strip().encode("utf-8")).digest()


async def record_trace(
    uow: UnitOfWork,
    *,
    question: str,
    surface: str,
    plan: dict[str, Any],
    packet: Packet,
    candidates: Sequence[dict[str, Any]],
    coverage: dict[str, Any],
    latency_ms: int,
) -> UUID:
    trace_id = uuid7()
    await uow.session.execute(
        insert(retrieval_traces_table).values(
            id=trace_id,
            user_id=uow.user_id,
            query_hash=question_hash(question),
            surface=surface,
            scenario=packet.scenario,
            planner=str(plan.get("planner", "fixed")),
            plan=plan,
            candidates=list(candidates)[:MAX_CANDIDATES],
            selected=[
                {
                    "cid": i.cid,
                    "key": i.key,
                    "section": i.section,
                    "score": round(i.score, 6),
                    "tokens": i.tokens,
                }
                for i in packet.items
            ],
            coverage=coverage,
            context_tokens=packet.tokens,
            budget_tokens=packet.budget.hard,
            latency_ms=latency_ms,
        )
    )
    return trace_id


async def purge_old_traces(uow: UnitOfWork, *, now: datetime.datetime) -> int:
    t = retrieval_traces_table
    result = await uow.session.execute(
        delete(t).where(t.c.user_id == uow.user_id, t.c.created_at < now - TRACE_RETENTION)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


async def purge_user_traces(uow: UnitOfWork) -> None:
    t = retrieval_traces_table
    await uow.session.execute(delete(t).where(t.c.user_id == uow.user_id))
