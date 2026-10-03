"""AI-10 meeting extraction orchestration (BACKEND_DESIGN.md §15 Phase 4 jobs, AI_PIPELINE.md §5.10).

``meeting_extract`` runs in natural-key mode on queue ``ai_standard``: transaction 1 (under
``mx:{recording}:{content_hash}:{prompt_version}``) moves the recording to ``extracting``, runs the
deterministic speaker matching, builds the input (participants, candidates from prior related
meetings and the participants, the full transcript) and claims the extraction key; the model is
called outside any transaction; transaction 2 stores the outcome. A succeeded key is reused
without a call; ``failed_permanent`` marks the recording ``failed`` at ``extract``; a budget
refusal defers the stage (not an attempt). Apply is the ``work.apply`` consumption handler.
"""

from __future__ import annotations

import datetime
from uuid import UUID

import structlog
from sqlalchemy import select, text

from eca import meetings
from eca.intelligence import (
    MEETING_PIPELINE,
    MEETING_PROMPT_VERSION,
    AIClient,
    Candidate,
    MeetingInput,
    MeetingParticipant,
    TranscriptLineIn,
    claim_meeting,
    ensure_completed_event,
    reopen_failed,
    run_meeting_extract,
    set_code_path,
    store_failure,
    store_success,
)
from eca.people import get_persons, get_self_person
from eca.platform.clock import Clock
from eca.platform.events import HandlerContext, handles
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory
from eca.work.candidates import matchable_items
from eca.work.models import decisions_table

log = structlog.get_logger("eca.work.meeting_pipeline")

MEETING_EXTRACT_HANDLER = "work.meeting_extract"
MEETING_EXTRACT_DUE_HANDLER = "work.meeting_extract_due"
MAX_ITEM_CANDIDATES = 8
MAX_DECISION_CANDIDATES = 4
CANDIDATE_WINDOW = datetime.timedelta(days=60)
_LOCK_SQL = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('mx:' || :rid || ':' || :hash || ':' || :version, 0))"
)


class MeetingExtractionRetry(RuntimeError):
    """Transient model failure below the attempt cap: the job is retried with backoff."""


def _now(ctx: HandlerContext) -> datetime.datetime:
    try:
        return ctx.resources.get(Clock).now()
    except LookupError:
        return datetime.datetime.now(datetime.UTC)


async def build_meeting_candidates(
    uow: UnitOfWork,
    *,
    participant_ids: set[UUID],
    prior_meeting_ids: list[UUID],
    self_id: UUID,
    at: datetime.datetime,
) -> list[Candidate]:
    """C1…C12 (CONTEXT_ARCHITECTURE.md §12.1): up to 8 open items of the participants and up to 4
    open questions or recent decisions of prior related meetings. Rejected items are flagged."""
    others = participant_ids - {self_id}
    window = at - CANDIDATE_WINDOW
    items = [
        i
        for i in await matchable_items(uow, now=at)
        if ({i.owner_person_id, i.counterparty_person_id} & others)
        and (i.last_activity_at is None or i.last_activity_at >= window)
    ]
    items.sort(
        key=lambda i: (
            i.verification_status == "rejected",
            -(i.last_activity_at or at).timestamp(),
            str(i.id),
        )
    )
    chosen = items[:MAX_ITEM_CANDIDATES]
    persons = await get_persons(
        uow, [p for i in chosen for p in (i.owner_person_id, i.counterparty_person_id) if p]
    )
    out: list[Candidate] = []
    for item in chosen:
        owner = persons.get(item.owner_person_id) if item.owner_person_id else None
        owner_name = (
            "the user"
            if owner and owner.is_self
            else (owner.display_name or owner.primary_email if owner else "unknown")
        )
        due = item.due_text or (item.due_at.date().isoformat() if item.due_at else "none")
        out.append(
            Candidate(
                code=f"C{len(out) + 1}",
                entity_type="work_item",
                entity_id=item.id,
                version=item.version,
                summary=f"{item.type}: {item.title}; owner {owner_name}; due {due}; "
                f"status {item.reported_status or item.lifecycle_status}",
                rejected=item.verification_status == "rejected",
            )
        )
    if prior_meeting_ids:
        d = decisions_table
        rows = (
            await uow.session.execute(
                select(d.c.id, d.c.kind, d.c.statement, d.c.version)
                .where(
                    d.c.user_id == uow.user_id,
                    d.c.meeting_id.in_(prior_meeting_ids),
                    d.c.deleted_at.is_(None),
                    d.c.merged_into_id.is_(None),
                    d.c.verification_status != "rejected",
                    d.c.resolved_by_id.is_(None),
                    d.c.superseded_by_id.is_(None),
                )
                .order_by((d.c.kind == "open_question").desc(), d.c.created_at.desc(), d.c.id)
                .limit(MAX_DECISION_CANDIDATES)
            )
        ).all()
        for r in rows:
            label = "open question" if r.kind == "open_question" else "decision"
            out.append(
                Candidate(
                    code=f"C{len(out) + 1}",
                    entity_type="decision",
                    entity_id=r.id,
                    version=r.version,
                    summary=f"{label} from a previous meeting: {r.statement}",
                )
            )
    return out


async def _input(
    uow: UnitOfWork, start: meetings.ExtractionStart, view: meetings.TranscriptView
) -> MeetingInput:
    record = await meetings.meeting_record(uow, start.meeting_id)
    assert record is not None
    self_p = await get_self_person(uow)
    mappings = await meetings.speaker_mappings(uow, start.meeting_id)
    refs = await get_persons(uow, [m.person_id for m in mappings])
    labels_of = {m.person_id: m.labels for m in mappings if m.status == "applied"}
    participants = tuple(
        MeetingParticipant(
            name=refs[m.person_id].display_name,
            email=refs[m.person_id].primary_email,
            is_self=refs[m.person_id].is_self,
            labels=tuple(labels_of.get(m.person_id, ())),
        )
        for m in mappings
        if m.person_id in refs and (m.participant_origin == "source" or m.status == "applied")
    )
    names = {
        label: refs[pid].display_name or refs[pid].primary_email
        for label, pid in view.speaker_people.items()
        if pid in refs
    }
    lines = tuple(
        TranscriptLineIn(s.seq, s.start_ms, s.speaker_label, names.get(s.speaker_label or ""), s.text)
        for s in view.segments
    )
    prior = [p.id for p in await meetings.prior_meetings(uow, start.meeting_id, limit=2)]
    candidates = await build_meeting_candidates(
        uow,
        participant_ids=set(record.participant_ids),
        prior_meeting_ids=prior,
        self_id=self_p.id,
        at=record.starts_at,
    )
    return MeetingInput(
        source_item_id=start.source_item_id,
        content_hash=start.transcript_hash,
        title=record.title,
        starts_at=record.starts_at,
        user_name=self_p.display_name or "the user",
        participants=participants,
        lines=lines,
        candidates=tuple(candidates),
    )


async def extract_meeting(
    uow_factory: UnitOfWorkFactory,
    client: AIClient,
    *,
    user_id: UUID,
    recording_id: UUID,
    now: datetime.datetime,
) -> str:
    """``skipped`` | ``reused`` | ``succeeded`` | ``deferred`` | ``failed_permanent``."""
    async with uow_factory(user_id=user_id) as uow:
        await set_code_path(uow, "extract")
        start = await meetings.begin_extraction(uow, recording_id, now=now)
        if start is None:
            return "skipped"
        await uow.session.execute(
            _LOCK_SQL,
            {
                "rid": str(recording_id),
                "hash": start.transcript_hash.hex(),
                "version": MEETING_PROMPT_VERSION,
            },
        )
        view = await meetings.current_transcript(uow, recording_id)
        if view is None:
            return "skipped"
        await meetings.deterministic_mapping(uow, start.meeting_id, list(view.segments), now=now)
        view = await meetings.current_transcript(uow, recording_id)
        assert view is not None
        inp = await _input(uow, start, view)
        claimed = await claim_meeting(uow, inp)
        if claimed.status == "failed_permanent" and start.user_retry:
            claimed = await reopen_failed(uow, claimed.extraction_id)
        if claimed.status == "succeeded":
            if claimed.apply_status == "pending":
                await ensure_completed_event(
                    uow, claimed.extraction_id, start.source_item_id, MEETING_PIPELINE
                )
            return "reused"
        if claimed.status == "failed_permanent":
            await meetings.extract_failed(uow, recording_id, error_code="extraction_failed_permanent")
            return "failed_permanent"
    outcome = await run_meeting_extract(client, inp, attempts_used=claimed.attempts, user_id=user_id)
    async with uow_factory(user_id=user_id) as uow:
        await set_code_path(uow, "extract")
        if outcome.deferred_for_s is not None:
            until = now + datetime.timedelta(seconds=outcome.deferred_for_s)
            await meetings.defer_extract(uow, recording_id, until=until, error_code="budget_deferred")
            log.info(
                "meeting_extraction_deferred", recording_id=str(recording_id), seconds=outcome.deferred_for_s
            )
            return "deferred"
        if outcome.ok:
            await store_success(uow, claimed, outcome)
            return "succeeded"
        status = await store_failure(uow, claimed, outcome)
        if status == "failed_permanent":
            await meetings.extract_failed(
                uow, recording_id, error_code=outcome.error_code or "extract_failed"
            )
            log.info(
                "meeting_extraction_failed", recording_id=str(recording_id), error_code=outcome.error_code
            )
            return "failed_permanent"
    raise MeetingExtractionRetry(outcome.error_code or "retryable model failure")


@handles(meetings.TRANSCRIPT_STORED, name=MEETING_EXTRACT_HANDLER, queue="ai_standard", mode="natural_key")
async def on_transcript_stored(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, meetings.TranscriptStored) and ctx.envelope.user_id is not None
    await extract_meeting(
        ctx.factory,
        ctx.resources.get(AIClient),
        user_id=ctx.envelope.user_id,
        recording_id=payload.recording_id,
        now=_now(ctx),
    )


@handles(
    meetings.RECORDING_STAGE_DUE, name=MEETING_EXTRACT_DUE_HANDLER, queue="ai_standard", mode="natural_key"
)
async def on_extract_due(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, meetings.RecordingStageDue) and ctx.envelope.user_id is not None
    if payload.stage == "extract":
        await extract_meeting(
            ctx.factory,
            ctx.resources.get(AIClient),
            user_id=ctx.envelope.user_id,
            recording_id=payload.recording_id,
            now=_now(ctx),
        )
