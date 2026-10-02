"""The index job (BACKEND_DESIGN.md §15 Phase 2 jobs; CONTEXT_ARCHITECTURE.md §9.9-§9.10, §12.7).

Natural-key mode, because AI-04 is an external call:

1. Transaction 1 (the user's): build the chunk drafts from the message or meeting, read the
   stored chunk hashes and the budget level.
2. No transaction: AI-04 for the chunks whose content hash or embedding model changed, at most
   ``EMBED_CALL_CAP`` calls per source version (job attempts), none at the hard budget cap.
3. Transaction 2, under ``pg_advisory_xact_lock('index:' || source_item_id)``: reload the drafts
   (a newer change wins), upsert chunks by ``(source_item_id, chunk_index)`` keeping a stored
   vector only while its content hash is unchanged, delete surplus chunks, replace the source's
   alias mentions and, for the first message of a conversation, write a ``continues`` link.

A failed embedding call leaves the chunks searchable by full text and retries the job.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

import structlog
from sqlalchemy import select, text
from sqlalchemy.exc import NoResultFound

from eca import communication, ingestion, intelligence, meetings, people, work
from eca.intelligence import AIClient, BudgetLevel, EmbeddingUnavailable
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory
from eca.retrieval.chunking import ChunkDraft, calendar_chunks, email_chunks
from eca.retrieval.mentions import AliasMatcher, AliasTarget
from eca.retrieval.models import chunks_table

log = structlog.get_logger("eca.retrieval.indexing")

EMBED_CALL_CAP = 4  # AI_PIPELINE.md §7: model calls per key, here per source version
PERSON_ALIAS_CONFIDENCE = 0.9
CONTINUATION_WINDOW = datetime.timedelta(days=30)
CONTINUATION_MIN_OVERLAP = 0.5
CONTINUATION_MIN_SUBJECT = 0.5
CONTINUATION_MIN_COSINE = 0.80
SUBJECT_PREFIX_RE = r"^(\s*(re|fw|fwd|aw|wg)\s*:\s*)+"

_INDEX_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended('index:' || :source_item_id, 0))")
_UPSERT_SQL = text(
    """
    INSERT INTO chunks (id, user_id, source_item_id, chunk_index, kind, title, text, token_count,
                        content_hash, occurred_at, conversation_id, meeting_id, person_ids,
                        embedding, embedding_model, embedded_at)
    VALUES (:id, :user_id, :source_item_id, :chunk_index, :kind, :title, :text, :token_count,
            :content_hash, :occurred_at, :conversation_id, :meeting_id, CAST(:person_ids AS uuid[]),
            CAST(:embedding AS halfvec), :embedding_model, :embedded_at)
    ON CONFLICT (source_item_id, chunk_index) DO UPDATE SET
        kind = EXCLUDED.kind, title = EXCLUDED.title, text = EXCLUDED.text,
        token_count = EXCLUDED.token_count, occurred_at = EXCLUDED.occurred_at,
        conversation_id = EXCLUDED.conversation_id, meeting_id = EXCLUDED.meeting_id,
        person_ids = EXCLUDED.person_ids,
        embedding = CASE WHEN EXCLUDED.embedding IS NOT NULL THEN EXCLUDED.embedding
                         WHEN chunks.content_hash = EXCLUDED.content_hash THEN chunks.embedding END,
        embedding_model = CASE
            WHEN EXCLUDED.embedding IS NOT NULL THEN EXCLUDED.embedding_model
            WHEN chunks.content_hash = EXCLUDED.content_hash THEN chunks.embedding_model END,
        embedded_at = CASE WHEN EXCLUDED.embedding IS NOT NULL THEN EXCLUDED.embedded_at
                           WHEN chunks.content_hash = EXCLUDED.content_hash THEN chunks.embedded_at END,
        content_hash = EXCLUDED.content_hash
    WHERE chunks.user_id = :user_id
    RETURNING id, chunk_index
    """
)
_DELETE_SURPLUS_SQL = text(
    "DELETE FROM chunks WHERE user_id = :user_id AND source_item_id = :source_item_id AND chunk_index >= :n"
)
_CONTINUATION_SQL = text(
    """
    WITH me AS (
      SELECT embedding, embedding_model FROM chunks
       WHERE user_id = :user_id AND source_item_id = :source_item_id AND chunk_index = 0)
    SELECT ch.conversation_id, ch.person_ids,
           similarity(regexp_replace(lower(coalesce(ch.title, '')), :prefix_re, ''), :subject) AS subject_sim,
           CASE WHEN me.embedding IS NOT NULL AND ch.embedding IS NOT NULL
                     AND ch.embedding_model = me.embedding_model
                THEN 1 - (ch.embedding <=> me.embedding) END AS cosine
      FROM chunks ch LEFT JOIN me ON true
     WHERE ch.user_id = :user_id AND ch.kind = 'email' AND ch.chunk_index = 0
       AND ch.conversation_id IS NOT NULL AND ch.conversation_id <> :conversation_id
       AND ch.occurred_at >= :since AND ch.occurred_at < :until
       AND ch.person_ids && CAST(:person_ids AS uuid[])
    """
)


class IndexRetry(RuntimeError):
    """The embedding call failed below the cap: the job is retried; chunks stay FTS-searchable."""


@dataclass(frozen=True)
class IndexTarget:
    source_item_id: UUID
    occurred_at: datetime.datetime
    conversation_id: UUID | None
    meeting_id: UUID | None
    person_ids: tuple[UUID, ...]  # participants without the user
    drafts: tuple[ChunkDraft, ...]
    first_in_conversation: bool = False
    subject_key: str = ""


@dataclass
class IndexReport:
    source_item_id: UUID | None
    chunks: int = 0
    embedded: int = 0
    mentions: int = 0
    linked: bool = False
    skipped: str | None = None
    embed_error: str | None = None
    call_ids: list[UUID] = field(default_factory=list)


TargetLoader = Callable[[UnitOfWork], Awaitable[IndexTarget | None]]


def subject_key(subject: str | None) -> str:
    return " ".join(re.sub(SUBJECT_PREFIX_RE, "", (subject or "").lower()).split())


async def message_target(uow: UnitOfWork, source_item_id: UUID) -> IndexTarget | None:
    """Chunk drafts of one normalized message; None when it is not (or no longer) a message."""
    item = await ingestion.get_source_item(uow, source_item_id)
    if item.kind != "message":
        return None
    try:
        view = await communication.get_message_view(uow, source_item_id)
    except NoResultFound:
        return None
    self_p = await people.get_self_person(uow)
    persons = sorted({p.id for p in (view.sender, *view.to, *view.cc) if p.id != self_p.id})
    order = await communication.conversation_source_items(uow, view.conversation_id)
    return IndexTarget(
        source_item_id=source_item_id,
        occurred_at=view.sent_at,
        conversation_id=view.conversation_id,
        meeting_id=None,
        person_ids=tuple(persons),
        drafts=tuple(email_chunks(view.subject, view.body_clean)),
        first_in_conversation=bool(order) and order[0] == source_item_id,
        subject_key=subject_key(view.subject),
    )


async def meeting_target(uow: UnitOfWork, meeting_id: UUID) -> IndexTarget | None:
    """Chunk draft of one calendar meeting; a cancelled meeting has none (its chunks go)."""
    found = await meetings.get_meeting_details(uow, [meeting_id])
    if not found:
        return None
    m = found[0]
    self_p = await people.get_self_person(uow)
    attendees = [p for p in m.attendee_ids if p != self_p.id]
    names = []
    for _, ref in sorted((await people.get_persons(uow, attendees)).items(), key=lambda kv: str(kv[0])):
        names.append(ref.display_name or ref.primary_email or "")
    drafts = () if m.status == "cancelled" else tuple(calendar_chunks(m.title, m.description, names))
    return IndexTarget(
        source_item_id=m.source_item_id,
        occurred_at=m.starts_at,
        conversation_id=None,
        meeting_id=m.id,
        person_ids=tuple(sorted(attendees)),
        drafts=drafts,
    )


async def _stored(uow: UnitOfWork, source_item_id: UUID) -> dict[int, tuple[bytes, str | None]]:
    t = chunks_table
    rows = await uow.session.execute(
        select(t.c.chunk_index, t.c.content_hash, t.c.embedding_model).where(
            t.c.user_id == uow.user_id, t.c.source_item_id == source_item_id
        )
    )
    return {r.chunk_index: (bytes(r.content_hash), r.embedding_model) for r in rows}


def _inputs(drafts: Sequence[ChunkDraft]) -> dict[int, str]:
    return {d.index: intelligence.document_input(d.title, d.text) for d in drafts}


def _model(client: AIClient) -> str | None:
    try:
        return intelligence.embedding_model(client)
    except intelligence.AIError:
        return None  # embed role disabled or unknown: FTS-only index (AI_PIPELINE.md §14)


async def alias_targets(uow: UnitOfWork) -> list[AliasTarget]:
    """Person aliases of the user (§12.7); confirmed project names are added in slice 2.5."""
    return [
        AliasTarget("person", a.person_id, a.alias, PERSON_ALIAS_CONFIDENCE)
        for a in await people.alias_catalog(uow)
    ]


async def _write(
    uow: UnitOfWork,
    target: IndexTarget,
    vectors: dict[int, tuple[bytes, tuple[float, ...]]],
    model: str | None,
    now: datetime.datetime,
) -> dict[int, UUID]:
    inputs = _inputs(target.drafts)
    ids: dict[int, UUID] = {}
    for d in target.drafts:
        digest = intelligence.input_digest(inputs[d.index])
        embedded = vectors.get(d.index)
        use = embedded is not None and embedded[0] == digest and model is not None
        row = (
            await uow.session.execute(
                _UPSERT_SQL,
                {
                    "id": uuid7(),
                    "user_id": uow.user_id,
                    "source_item_id": target.source_item_id,
                    "chunk_index": d.index,
                    "kind": d.kind,
                    "title": d.title,
                    "text": d.text,
                    "token_count": d.token_count,
                    "content_hash": digest,
                    "occurred_at": target.occurred_at,
                    "conversation_id": target.conversation_id,
                    "meeting_id": target.meeting_id,
                    "person_ids": list(target.person_ids),
                    "embedding": intelligence.vector_literal(embedded[1]) if use and embedded else None,
                    "embedding_model": model if use else None,
                    "embedded_at": now if use else None,
                },
            )
        ).one()
        ids[row.chunk_index] = row.id
    await uow.session.execute(
        _DELETE_SURPLUS_SQL,
        {"user_id": uow.user_id, "source_item_id": target.source_item_id, "n": len(target.drafts)},
    )
    return ids


async def _mentions(uow: UnitOfWork, target: IndexTarget, chunk_ids: dict[int, UUID]) -> int:
    matcher = AliasMatcher(await alias_targets(uow))
    mentions: list[people.MentionIn] = []
    for d in target.drafts:
        for hit in matcher.scan(d.text):
            mentions.append(
                people.MentionIn(
                    target.source_item_id,
                    hit.entity_type,
                    hit.entity_id,
                    hit.surface_text,
                    hit.confidence,
                    "alias_match",
                    target.occurred_at,
                    chunk_id=chunk_ids.get(d.index),
                )
            )
    await people.replace_alias_mentions(uow, target.source_item_id, mentions)
    return len(mentions)


def overlap(a: Sequence[UUID], b: Sequence[UUID]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


async def _continuation(uow: UnitOfWork, target: IndexTarget) -> bool:
    """§12.7: link the first message's conversation to the best earlier conversation."""
    if not target.first_in_conversation or target.conversation_id is None or not target.person_ids:
        return False
    rows = (
        await uow.session.execute(
            _CONTINUATION_SQL,
            {
                "user_id": uow.user_id,
                "source_item_id": target.source_item_id,
                "conversation_id": target.conversation_id,
                "prefix_re": SUBJECT_PREFIX_RE,
                "subject": target.subject_key,
                "since": target.occurred_at - CONTINUATION_WINDOW,
                "until": target.occurred_at,
                "person_ids": list(target.person_ids),
            },
        )
    ).all()
    grouped: dict[UUID, tuple[set[UUID], float, float | None]] = {}
    for r in rows:
        persons, subj, cos = grouped.get(r.conversation_id, (set(), 0.0, None))
        persons |= set(r.person_ids or ())
        subj = max(subj, float(r.subject_sim or 0.0))
        if r.cosine is not None:
            cos = max(cos or 0.0, float(r.cosine))
        grouped[r.conversation_id] = (persons, subj, cos)
    best: tuple[float, str, UUID, float, float | None] | None = None
    for conv_id, (persons, subj, cos) in grouped.items():
        if overlap(target.person_ids, sorted(persons)) < CONTINUATION_MIN_OVERLAP:
            continue
        subject_ok = bool(target.subject_key) and subj >= CONTINUATION_MIN_SUBJECT
        body_ok = cos is not None and cos >= CONTINUATION_MIN_COSINE
        if not (subject_ok or body_ok):
            continue
        score = max(subj if subject_ok else 0.0, cos if body_ok and cos is not None else 0.0)
        candidate = (score, str(conv_id), conv_id, subj, cos)
        if best is None or (candidate[0], candidate[1]) > (best[0], best[1]):
            best = candidate
    if best is None:
        return False
    score, _, conv_id, subj, cos = best
    body_won = cos is not None and cos >= CONTINUATION_MIN_COSINE and cos >= subj
    return await work.link_entities(
        uow,
        from_type="conversation",
        from_id=target.conversation_id,
        to_type="conversation",
        to_id=conv_id,
        relation="continues",
        confidence=score,
        method="embedding_match" if body_won else "deterministic",
        origin="computed",
        scores={"subject_similarity": round(subj, 4), "cosine": None if cos is None else round(cos, 4)},
    )


async def index_source(
    factory: UnitOfWorkFactory,
    client: AIClient,
    *,
    user_id: UUID,
    load: TargetLoader,
    attempt: int,
    now: datetime.datetime,
) -> IndexReport:
    """Index one source item (message or meeting). ``attempt`` = earlier runs of this job."""
    async with factory(user_id=user_id) as uow:
        target = await load(uow)
        if target is None:
            return IndexReport(None, skipped="not_indexable")
        stored = await _stored(uow, target.source_item_id)
        level = await intelligence.budget_level(uow, now=now)
    report = IndexReport(target.source_item_id)
    inputs = _inputs(target.drafts)
    model = _model(client)
    need = [
        i
        for i in sorted(inputs)
        if model is not None and stored.get(i) != (intelligence.input_digest(inputs[i]), model)
    ]
    vectors: dict[int, tuple[bytes, tuple[float, ...]]] = {}
    if need and model is not None:
        if level is BudgetLevel.HARD:
            report.skipped = "budget_hard_cap"
        elif attempt >= EMBED_CALL_CAP:
            report.skipped = "embed_attempt_cap"
        else:
            try:
                result = await intelligence.embed_documents(
                    client, [inputs[i] for i in need], user_id=user_id, attempt=attempt + 1
                )
            except EmbeddingUnavailable as exc:
                report.embed_error = exc.error_type
            else:
                model = result.model
                vectors = {
                    i: (intelligence.input_digest(inputs[i]), v)
                    for i, v in zip(need, result.vectors, strict=True)
                }
                report.call_ids.extend(result.call_ids)
    async with factory(user_id=user_id) as uow:
        await uow.session.execute(_INDEX_LOCK_SQL, {"source_item_id": str(target.source_item_id)})
        # A newer change of the same source wins: its text, and our vectors only where equal.
        fresh = await load(uow)
        if fresh is None:
            await purge_sources(uow, [target.source_item_id])
            report.skipped = "removed"
            return report
        chunk_ids = await _write(uow, fresh, vectors, model, now)
        report.chunks = len(chunk_ids)
        report.embedded = len(vectors)
        report.mentions = await _mentions(uow, fresh, chunk_ids)
        report.linked = await _continuation(uow, fresh)
    log.info(
        "source_indexed",
        source_item_id=str(target.source_item_id),
        chunks=report.chunks,
        embedded=report.embedded,
        mentions=report.mentions,
        linked=report.linked,
        skipped=report.skipped,
        embed_error=report.embed_error,
    )
    if report.embed_error is not None and attempt + 1 < EMBED_CALL_CAP:
        raise IndexRetry(report.embed_error)
    return report


async def remove_source(uow: UnitOfWork, source_item_id: UUID) -> None:
    """Provider deletion (BACKEND_DESIGN.md §9.3 step 1): chunks and alias mentions go."""
    await uow.session.execute(_INDEX_LOCK_SQL, {"source_item_id": str(source_item_id)})
    await purge_sources(uow, [source_item_id])


async def purge_sources(uow: UnitOfWork, source_item_ids: Sequence[UUID]) -> None:
    ids = list(source_item_ids)
    if not ids:
        return
    for source_item_id in ids:
        await people.replace_alias_mentions(uow, source_item_id, [])
    t = chunks_table
    await uow.session.execute(t.delete().where(t.c.user_id == uow.user_id, t.c.source_item_id.in_(ids)))


async def purge_unretained(uow: UnitOfWork) -> int:
    """Retention: chunks of messages whose body is no longer stored are deleted."""
    t = chunks_table
    sources = [
        r.source_item_id
        for r in await uow.session.execute(
            select(t.c.source_item_id).where(t.c.user_id == uow.user_id, t.c.kind == "email").distinct()
        )
    ]
    gone = sorted(await communication.sources_without_body(uow, sources))
    await purge_sources(uow, gone)
    return len(gone)


@dataclass(frozen=True)
class ReembedReport:
    stale: int
    embedded: int


async def reembed_stale(
    factory: UnitOfWorkFactory,
    client: AIClient,
    *,
    user_id: UUID,
    limit: int,
    dry_run: bool,
    now: datetime.datetime,
) -> ReembedReport:
    """Operator path for an embedding model change (AI_PIPELINE.md §10): never scheduled."""
    model = intelligence.embedding_model(client)
    t = chunks_table
    async with factory(user_id=user_id) as uow:
        rows = (
            await uow.session.execute(
                select(t.c.id, t.c.title, t.c.text, t.c.content_hash)
                .where(t.c.user_id == user_id, t.c.embedding_model.is_distinct_from(model))
                .order_by(t.c.occurred_at.desc(), t.c.id)
                .limit(limit)
            )
        ).all()
    if dry_run or not rows:
        return ReembedReport(len(rows), 0)
    inputs = [intelligence.document_input(r.title, r.text) for r in rows]
    result = await intelligence.embed_documents(client, inputs, user_id=user_id)
    written = 0
    async with factory(user_id=user_id) as uow:
        for r, vector in zip(rows, result.vectors, strict=True):
            updated = await uow.session.execute(
                text(
                    "UPDATE chunks SET embedding = CAST(:embedding AS halfvec), embedding_model = :model, "
                    "embedded_at = :now WHERE user_id = :user_id AND id = :id AND content_hash = :hash"
                ),
                {
                    "embedding": intelligence.vector_literal(vector),
                    "model": result.model,
                    "now": now,
                    "user_id": user_id,
                    "id": r.id,
                    "hash": bytes(r.content_hash),
                },
            )
            written += int(updated.rowcount)  # type: ignore[attr-defined]
    return ReembedReport(len(rows), written)
