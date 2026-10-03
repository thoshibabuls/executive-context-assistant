"""Apply a stored AI-10 meeting extraction (AI_PIPELINE.md §5.10 "Apply"). Deterministic; never
calls a model. One transaction under the per-user merge lock, like email apply (§8.2):

1. AI speaker proposals resolved to persons and passed to ``meetings.record_ai_mappings``
   (auto-apply only at ≥ 0.9; user mappings are never replaced). Applied mappings are read after.
2. Statements: the evidence segment must exist in the transcript version and the quote must occur
   in it (or in it joined with the next segment); a whitespace/case-insensitive match or model
   timestamps outside the segment ± 2 s add the fuzzy penalty. Stored evidence uses the segment's
   own offsets, ``occurred_at`` = meeting start + ``start_ms``. Mapping §5.5 with speaker = the
   label's applied person; an unmapped label gives direction ``unresolved``.
3. Status signals and decisions (``resolves``, ``supersedes``, ``contradicts``) with
   ``context_events``; concerns only when grounded.
4. Summary with provenance through ``meetings.store_summary``; ``meetings.mark_processed``.

User-authored values (authority 5) win in the fold; decision links the user set are not changed.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from eca import meetings
from eca.identity import get_user_settings
from eca.intelligence import (
    MEETING_PROMPT_VERSION,
    ExtractionRecord,
    MeetingExtraction,
    MeetingSignal,
    SegmentEvidence,
    get_extraction,
    mark_applied,
    set_code_path,
    succeeded_for_source,
)
from eca.people import (
    MentionIn,
    PersonRef,
    find_by_email,
    get_persons,
    get_self_person,
    match_names,
    record_mentions,
    resolve_name,
)
from eca.platform.uow import UnitOfWork
from eca.work import confidence as conf
from eca.work.apply import MERGE_LOCK_SQL, REPORTED, ApplyReport
from eca.work.candidates import OpenItem, match_items, matchable_items
from eca.work.dates import DueResolution, guess_disagrees, resolve_due
from eca.work.mapping import authority, map_statement
from eca.work.models import decisions_table, item_evidence_table, work_items_table
from eca.work.queries import get_evidence
from eca.work.service import (
    Relink,
    add_evidence,
    append_event,
    create_item,
    evidence_id_for,
    link_evidence,
    model_dedupe_key,
    record_entity_event,
    replay_on_kept_item,
    user_dedupe_key,
)

log = structlog.get_logger("eca.work.meeting_apply")

SIGNAL_INDEX = 100
DECISION_INDEX = 200
CONCERN_INDEX = 300
DECISION_ID_INDEX = 1000
TIMESTAMP_SLACK_MS = 2000
NAME_MATCH_MIN = 0.9


@dataclass(frozen=True)
class GroundedSegment:
    quote: str
    seq: int
    start_ms: int | None
    end_ms: int | None
    fuzzy: bool


def _contains(quote: str, body: str) -> tuple[bool, bool]:
    """(found, fuzzy): verbatim, else whitespace and case-insensitive."""
    if quote in body:
        return True, False
    norm_quote = " ".join(quote.split()).lower()
    return bool(norm_quote) and norm_quote in " ".join(body.split()).lower(), True


def ground_segment(
    ev: SegmentEvidence, segments: dict[int, meetings.Segment], *, duration_ms: int | None
) -> GroundedSegment | None:
    """The quote must occur in the cited segment, or in it joined with the next one."""
    seg = segments.get(ev.segment_seq)
    if seg is None:
        return None
    nxt = segments.get(ev.segment_seq + 1)
    bodies = [seg.text] + ([f"{seg.text} {nxt.text}"] if nxt is not None else [])
    for body in bodies:
        found, fuzzy = _contains(ev.quote, body)
        if not found:
            continue
        if (
            ev.start_ms is not None
            and seg.start_ms is not None
            and abs(ev.start_ms - seg.start_ms) > TIMESTAMP_SLACK_MS
        ):
            fuzzy = True
        if (
            ev.start_ms is not None
            and duration_ms is not None
            and ev.start_ms > duration_ms + TIMESTAMP_SLACK_MS
        ):
            fuzzy = True
        end = (nxt.end_ms if body is not bodies[0] and nxt is not None else seg.end_ms) or seg.end_ms
        return GroundedSegment(ev.quote, seg.seq, seg.start_ms, end, fuzzy)
    return None


def at(start: datetime.datetime, ms: int | None) -> datetime.datetime:
    return start + datetime.timedelta(milliseconds=ms or 0)


async def resolve_ref(
    uow: UnitOfWork,
    ref: str | None,
    *,
    speaker: PersonRef | None,
    self_p: PersonRef,
    by_label: dict[str, PersonRef],
    participants: list[PersonRef],
) -> PersonRef | None:
    if ref is None or ref == "unknown":
        return None
    if ref == "speaker":
        return speaker
    if ref == "self":
        return self_p
    kind, _, value = ref.partition(":")
    if kind == "speaker":
        return by_label.get(value.strip())
    if kind == "name":
        return await resolve_name(uow, value, participants)
    if kind == "email":
        return await find_by_email(uow, value.strip())
    return None


async def proposal_person(
    uow: UnitOfWork, email: str | None, name: str | None, participants: list[PersonRef]
) -> PersonRef | None:
    """AI-10 speaker proposals resolve by email, else by name among the attendees, else by a
    unique strong name match among all persons."""
    if email:
        found = await find_by_email(uow, email)
        if found is not None:
            return found
    if name:
        found = await resolve_name(uow, name, participants)
        if found is not None:
            return found
        matches = await match_names(uow, name, limit=2)
        if len(matches) == 1 and matches[0].score >= NAME_MATCH_MIN:
            return matches[0].person
    return None


def _due_fields(res: DueResolution) -> dict[str, Any]:
    if res.due_text is None:
        return {}
    return {
        "due_at": res.due_at,
        "due_precision": res.precision,
        "due_text": res.due_text,
        "due_kind": res.kind,
    }


def _json(sets: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in sets.items():
        out[k] = v.isoformat() if isinstance(v, datetime.datetime) else (str(v) if isinstance(v, UUID) else v)
    return out


def _candidate(ext: ExtractionRecord, code: str | None, entity_type: str) -> UUID | None:
    entry = ext.candidate_map.get(code or "")
    if not entry or entry.get("entity_type") != entity_type:
        return None
    return UUID(entry["entity_id"])


@dataclass
class _Context:
    ext: ExtractionRecord
    out: MeetingExtraction
    meeting: meetings.MeetingRecord
    segments: dict[int, meetings.Segment]
    duration_ms: int | None
    self_p: PersonRef
    by_label: dict[str, PersonRef]
    participants: list[PersonRef]
    tz: str
    now: datetime.datetime
    relink: Mapping[UUID, Relink] = field(default_factory=dict)  # R2 only (BACKEND_DESIGN.md §8.5)


async def _statements(uow: UnitOfWork, c: _Context, report: ApplyReport) -> None:
    items = await matchable_items(uow, now=c.meeting.starts_at)
    for idx, st in enumerate(c.out.statements):
        g = ground_segment(st.evidence, c.segments, duration_ms=c.duration_ms)
        if g is None:
            report.dropped_ungrounded += 1
            continue
        speaker = c.by_label.get(st.speaker)
        owner = await resolve_ref(
            uow,
            st.owner_ref,
            speaker=speaker,
            self_p=c.self_p,
            by_label=c.by_label,
            participants=c.participants,
        )
        beneficiary = await resolve_ref(
            uow,
            st.beneficiary_ref,
            speaker=speaker,
            self_p=c.self_p,
            by_label=c.by_label,
            participants=c.participants,
        )
        addressees = tuple(p.id for p in c.participants if speaker is None or p.id != speaker.id)
        mapped = map_statement(
            kind=st.statement_kind,
            speaker_id=speaker.id if speaker else None,
            self_id=c.self_p.id,
            owner_id=owner.id if owner else None,
            beneficiary_id=beneficiary.id if beneficiary else None,
            addressees=addressees,
        )
        occurred = at(c.meeting.starts_at, g.start_ms)
        due_text = st.due_text if st.due_text and st.due_text in g.quote else None
        due = resolve_due(due_text, reference=c.meeting.starts_at, timezone=c.tz)
        penalties = []
        if g.fuzzy:
            penalties.append("fuzzy_grounding")
        if mapped.owner_id is None:
            penalties.append("owner_unresolved")
        if guess_disagrees(due, st.due_iso_guess):
            penalties.append("date_disagreement")
        if st.statement_kind.startswith("report_"):
            penalties.append("report_statement")
        if mapped.owner_inconsistent:
            penalties.append("owner_inconsistent")
        score = conf.compute(st.confidence, penalties)
        ev_id = evidence_id_for(c.ext.id, idx)
        auth = authority(
            speaker_id=speaker.id if speaker else None, owner_id=mapped.owner_id, strength=mapped.strength
        )
        kept = c.relink.get(ev_id)
        if kept is not None:
            await add_evidence(
                uow,
                evidence_id=ev_id,
                source_item_id=c.ext.source_item_id,
                extraction_id=c.ext.id,
                index=idx,
                quote=g.quote,
                char_start=None,
                occurred_at=occurred,
                start_ms=g.start_ms,
                end_ms=g.end_ms,
            )
            due_sets = _json(_due_fields(due))
            await replay_on_kept_item(
                uow,
                kept,
                ev_id=ev_id,
                extraction_id=c.ext.id,
                index=idx,
                authority=auth,
                occurred_at=occurred,
                by_person=speaker.id if speaker else None,
                confidence=score.value,
                penalties=list(score.penalties),
                created_sets=_json(
                    {
                        "type": mapped.type,
                        "title": st.action,
                        "direction": mapped.direction,
                        "owner_person_id": mapped.owner_id,
                        "counterparty_person_id": mapped.counterparty_id,
                        "requester_person_id": mapped.requester_id,
                        "commitment_strength": mapped.strength,
                        "statement_kind": st.statement_kind,
                        "confidence": score.value,
                        "confidence_band": score.band,
                        "project_hint": c.out.project_hint,
                        **_due_fields(due),
                    }
                ),
                due_sets=due_sets,
                acceptance_sets=_json(
                    {
                        "type": "commitment",
                        "statement_kind": "acceptance",
                        "direction": mapped.direction,
                        "commitment_strength": "explicit",
                        **_due_fields(due),
                    }
                ),
            )
            report.updated.append(kept.item_id)
            continue
        target_id = _candidate(c.ext, st.candidate_id, "work_item")
        ambiguous: list[Any] = []
        if target_id is None:
            match, ambiguous = match_items(
                items,
                type_=mapped.type,
                title=st.action,
                owner_id=mapped.owner_id,
                counterparty_id=mapped.counterparty_id,
                due_at=due.due_at,
            )
            target_id = match.item.id if match else None
        target = next((i for i in items if i.id == target_id), None)
        owner_restates = (
            mapped.strength == "explicit"
            and speaker is not None
            and speaker.id == (target.owner_person_id if target else None)
        )
        if target is not None and target.verification_status == "rejected" and not owner_restates:
            report.dropped_rejected += 1
            continue
        if score.value < conf.MIN_TO_CREATE and target is None:
            report.dropped_low_confidence += 1
            continue
        await add_evidence(
            uow,
            evidence_id=ev_id,
            source_item_id=c.ext.source_item_id,
            extraction_id=c.ext.id,
            index=idx,
            quote=g.quote,
            char_start=None,
            occurred_at=occurred,
            start_ms=g.start_ms,
            end_ms=g.end_ms,
        )
        if target is not None:
            sets: dict[str, Any] = {}
            event_type = "restated"
            if st.statement_kind == "acceptance":
                event_type = "accepted"
                sets = {
                    "type": "commitment",
                    "statement_kind": "acceptance",
                    "direction": mapped.direction,
                    "commitment_strength": "explicit",
                    **_due_fields(due),
                }
            elif due.due_text is not None:
                event_type = "due_changed"
                sets = _due_fields(due)
            await append_event(
                uow,
                item_id=target.id,
                event_type=event_type,
                actor="model",
                authority=auth,
                materiality=3 if event_type == "due_changed" else 1,
                occurred_at=occurred,
                dedupe_key=model_dedupe_key(c.ext.id, idx, event_type),
                payload={
                    "set": _json(sets),
                    "by_person": str(speaker.id) if speaker else None,
                    "speaker_label": st.speaker,
                    "confidence": score.value,
                    "penalties": list(score.penalties),
                },
                evidence_id=ev_id,
                extraction_id=c.ext.id,
            )
            await link_evidence(uow, "work_item", target.id, ev_id, "updates" if sets else "supports")
            report.updated.append(target.id)
            continue
        fields: dict[str, Any] = {
            "type": mapped.type,
            "title": st.action,
            "direction": mapped.direction,
            "owner_person_id": mapped.owner_id,
            "counterparty_person_id": mapped.counterparty_id,
            "requester_person_id": mapped.requester_id,
            "commitment_strength": mapped.strength,
            "statement_kind": st.statement_kind,
            "confidence": score.value if not ambiguous else min(score.value, 0.59),
            "confidence_band": score.band if not ambiguous else "low",
            "project_hint": c.out.project_hint,
            **_due_fields(due),
        }
        item_id = await create_item(
            uow,
            fields=fields,
            origin="ai",
            extraction_id=c.ext.id,
            method="llm",
            model=c.ext.model,
            derived_at=c.now,
            actor="model",
            authority=auth,
            materiality=2,
            occurred_at=occurred,
            dedupe_key=model_dedupe_key(c.ext.id, idx, "created"),
            evidence_id=ev_id,
            pending_adjudication=False,
            by_person=speaker.id if speaker else None,
        )
        report.created.append(item_id)
        items.append(
            OpenItem(
                item_id,
                mapped.type,
                st.action,
                mapped.owner_id,
                mapped.counterparty_id,
                due.due_at,
                due.due_text,
                "open",
                "suggested",
                None,
                1,
                occurred,
            )
        )


def _signal_event(
    sig: MeetingSignal,
    quote: str,
    occurred: datetime.datetime,
    ev_id: UUID,
    *,
    reference: datetime.datetime,
    tz: str,
) -> tuple[str, dict[str, Any]]:
    """(event type, fields) of a status signal on an item (shared by apply and speaker re-pointing)."""
    sets: dict[str, Any] = {}
    event_type = "status_signal"
    if sig.signal == "new_deadline":
        text = sig.new_due_text if sig.new_due_text and sig.new_due_text in quote else None
        sets = _due_fields(resolve_due(text, reference=reference, timezone=tz))
        event_type = "due_changed"
    elif sig.signal == "accepted":
        sets = {"type": "commitment", "statement_kind": "acceptance", "commitment_strength": "explicit"}
        event_type = "accepted"
    elif sig.signal in REPORTED:
        sets = {
            "reported_status": REPORTED[sig.signal],
            "reported_status_at": occurred,
            "reported_status_evidence_id": ev_id,
        }
        if sig.signal == "completed_claim":
            event_type = "completed_claim"
        if sig.signal == "cancelled":
            sets["lifecycle_status"] = "cancelled"  # the fold turns this into a prompt, never a fact
    return event_type, sets


def signal_authority(speaker_id: UUID | None, owner_id: UUID | None) -> int:
    """CONTEXT_ARCHITECTURE.md §8.2: 4 the owner, 3 another mapped participant, 2 unmapped speaker."""
    if speaker_id is None:
        return 2
    return 4 if speaker_id == owner_id else 3


async def _owner_of(uow: UnitOfWork, item_id: UUID) -> UUID | None:
    t = work_items_table
    return (
        await uow.session.execute(select(t.c.owner_person_id).where(t.c.id == item_id))
    ).scalar_one_or_none()


async def _signals(uow: UnitOfWork, c: _Context, report: ApplyReport, decided: set[str]) -> None:
    for n, sig in enumerate(c.out.status_signals):
        g = ground_segment(sig.evidence, c.segments, duration_ms=c.duration_ms)
        if g is None:
            report.dropped_ungrounded += 1
            continue
        idx = SIGNAL_INDEX + n
        ev_id = evidence_id_for(c.ext.id, idx)
        occurred = at(c.meeting.starts_at, g.start_ms)
        question_id = _candidate(c.ext, sig.candidate_id, "decision")
        if question_id is not None:
            # A resolved open question without a decision in the output: the resolving decision is
            # created from the grounded quote (AI_PIPELINE.md §5.10).
            if sig.signal == "resolved" and sig.candidate_id not in decided:
                await add_evidence(
                    uow,
                    evidence_id=ev_id,
                    source_item_id=c.ext.source_item_id,
                    extraction_id=c.ext.id,
                    index=idx,
                    quote=g.quote,
                    char_start=None,
                    occurred_at=occurred,
                    start_ms=g.start_ms,
                    end_ms=g.end_ms,
                )
                new_id = await _insert_decision(
                    uow,
                    c,
                    n=DECISION_ID_INDEX + idx,
                    kind="decision",
                    statement=g.quote[:300],
                    score=sig.confidence,
                    fuzzy=g.fuzzy,
                    occurred=occurred,
                    ev_id=ev_id,
                    idx=idx,
                )
                await _relate(uow, c, new_id, question_id, "resolves", occurred, idx)
            continue
        target_id = _candidate(c.ext, sig.candidate_id, "work_item")
        if target_id is None:
            report.dropped_ungrounded += 1
            continue
        await add_evidence(
            uow,
            evidence_id=ev_id,
            source_item_id=c.ext.source_item_id,
            extraction_id=c.ext.id,
            index=idx,
            quote=g.quote,
            char_start=None,
            occurred_at=occurred,
            start_ms=g.start_ms,
            end_ms=g.end_ms,
        )
        speaker = c.by_label.get(sig.speaker)
        event_type, sets = _signal_event(
            sig, g.quote, occurred, ev_id, reference=c.meeting.starts_at, tz=c.tz
        )
        await append_event(
            uow,
            item_id=target_id,
            event_type=event_type,
            actor="model",
            authority=signal_authority(speaker.id if speaker else None, await _owner_of(uow, target_id)),
            materiality=3 if event_type in ("due_changed", "completed_claim") else 1,
            occurred_at=occurred,
            dedupe_key=model_dedupe_key(c.ext.id, idx, event_type),
            payload={
                "set": _json(sets),
                "by_person": str(speaker.id) if speaker else None,
                "speaker_label": sig.speaker,
                "signal": sig.signal,
                "confidence": sig.confidence,
            },
            evidence_id=ev_id,
            extraction_id=c.ext.id,
        )
        await link_evidence(
            uow, "work_item", target_id, ev_id, "completes" if sig.signal == "completed_claim" else "updates"
        )
        report.updated.append(target_id)


async def _insert_decision(
    uow: UnitOfWork,
    c: _Context,
    *,
    n: int,
    kind: str,
    statement: str,
    score: float,
    fuzzy: bool,
    occurred: datetime.datetime,
    ev_id: UUID,
    idx: int,
) -> UUID:
    value = conf.compute(score, ["fuzzy_grounding"] if fuzzy else [])
    decision_id = evidence_id_for(c.ext.id, n)  # deterministic, so a re-apply finds the same row
    await uow.session.execute(
        insert(decisions_table)
        .values(
            id=decision_id,
            user_id=uow.user_id,
            kind=kind,
            statement=statement,
            decided_at=occurred if kind == "decision" else None,
            meeting_id=c.meeting.id,
            project_hint=c.out.project_hint,
            origin="ai",
            verification_status="suggested",
            confidence=value.value,
            confidence_band=value.band,
            extraction_id=c.ext.id,
            extraction_method="llm",
            model=c.ext.model,
            derived_at=c.now,
            user_fields=[],
            version=1,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await link_evidence(uow, "decision", decision_id, ev_id, "supports")
    await record_entity_event(
        uow,
        entity_type="decision",
        entity_id=decision_id,
        event_type="created",
        actor="model",
        authority=2,
        materiality=3 if kind == "decision" else 2,
        occurred_at=occurred,
        dedupe_key=model_dedupe_key(c.ext.id, idx, "decision_created"),
        payload={"kind": kind, "meeting_id": str(c.meeting.id), "evidence_id": str(ev_id)},
    )
    return decision_id


async def _relate(
    uow: UnitOfWork,
    c: _Context,
    decision_id: UUID,
    target_id: UUID,
    relation: str,
    occurred: datetime.datetime,
    idx: int,
) -> None:
    """``resolves`` an open question, ``supersedes`` a decision, or ``contradicts`` one (CC-39).
    A link the user set (``user_fields``) is never replaced."""
    d = decisions_table
    target = (
        await uow.session.execute(
            select(d.c.kind, d.c.resolved_by_id, d.c.superseded_by_id, d.c.user_fields).where(
                d.c.user_id == uow.user_id, d.c.id == target_id, d.c.deleted_at.is_(None)
            )
        )
    ).one_or_none()
    if target is None or target_id == decision_id:
        return
    user_fields = set(target.user_fields or [])
    if relation == "resolves" and target.kind == "open_question":
        if target.resolved_by_id is not None or "resolved_by_id" in user_fields:
            return
        await uow.session.execute(
            update(d).where(d.c.id == target_id).values(resolved_by_id=decision_id, version=d.c.version + 1)
        )
        event = "resolved"
    elif relation == "supersedes" and target.kind == "decision":
        if target.superseded_by_id is not None or "superseded_by_id" in user_fields:
            return
        await uow.session.execute(
            update(d).where(d.c.id == target_id).values(superseded_by_id=decision_id, version=d.c.version + 1)
        )
        event = "superseded"
    elif relation == "contradicts":
        for entity_id, other in ((target_id, decision_id), (decision_id, target_id)):
            await record_entity_event(
                uow,
                entity_type="decision",
                entity_id=entity_id,
                event_type="conflict_detected",
                actor="model",
                authority=2,
                materiality=3,
                occurred_at=occurred,
                dedupe_key=model_dedupe_key(c.ext.id, idx, f"conflict:{entity_id}"),
                payload={"with_decision": str(other), "meeting_id": str(c.meeting.id)},
            )
        return
    else:
        return
    await record_entity_event(
        uow,
        entity_type="decision",
        entity_id=target_id,
        event_type=event,
        actor="model",
        authority=2,
        materiality=3,
        occurred_at=occurred,
        dedupe_key=model_dedupe_key(c.ext.id, idx, event),
        payload={"by_decision": str(decision_id), "meeting_id": str(c.meeting.id)},
    )


async def _decisions(uow: UnitOfWork, c: _Context, report: ApplyReport) -> set[str]:
    decided: set[str] = set()
    for n, dec in enumerate(c.out.decisions):
        g = ground_segment(dec.evidence, c.segments, duration_ms=c.duration_ms)
        if g is None:
            report.dropped_ungrounded += 1
            continue
        idx = DECISION_INDEX + n
        ev_id = evidence_id_for(c.ext.id, idx)
        occurred = at(c.meeting.starts_at, g.start_ms)
        await add_evidence(
            uow,
            evidence_id=ev_id,
            source_item_id=c.ext.source_item_id,
            extraction_id=c.ext.id,
            index=idx,
            quote=g.quote,
            char_start=None,
            occurred_at=occurred,
            start_ms=g.start_ms,
            end_ms=g.end_ms,
        )
        decision_id = await _insert_decision(
            uow,
            c,
            n=DECISION_ID_INDEX + n,
            kind=dec.kind,
            statement=dec.statement,
            score=dec.confidence,
            fuzzy=g.fuzzy,
            occurred=occurred,
            ev_id=ev_id,
            idx=idx,
        )
        target_id = _candidate(c.ext, dec.candidate_id, "decision")
        if target_id is not None and dec.relation != "none":
            await _relate(uow, c, decision_id, target_id, dec.relation, occurred, idx)
            if dec.candidate_id:
                decided.add(dec.candidate_id)
    return decided


async def _concerns(uow: UnitOfWork, c: _Context) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, concern in enumerate(c.out.concerns):
        g = ground_segment(concern.evidence, c.segments, duration_ms=c.duration_ms)
        if g is None:
            continue  # concerns are kept only when grounded
        idx = CONCERN_INDEX + n
        ev_id = evidence_id_for(c.ext.id, idx)
        await add_evidence(
            uow,
            evidence_id=ev_id,
            source_item_id=c.ext.source_item_id,
            extraction_id=c.ext.id,
            index=idx,
            quote=g.quote,
            char_start=None,
            occurred_at=at(c.meeting.starts_at, g.start_ms),
            start_ms=g.start_ms,
            end_ms=g.end_ms,
        )
        out.append({"text": concern.text, "evidence_id": str(ev_id), "start_ms": g.start_ms})
    return out


async def _people(
    uow: UnitOfWork, meeting_id: UUID
) -> tuple[meetings.MeetingRecord, dict[str, PersonRef], list[PersonRef]]:
    record = await meetings.meeting_record(uow, meeting_id)
    assert record is not None
    labels = await meetings.applied_labels(uow, meeting_id)
    refs = await get_persons(uow, set(labels.values()) | set(record.participant_ids))
    by_label = {label: refs[pid] for label, pid in labels.items() if pid in refs}
    participants = [refs[pid] for pid in record.participant_ids if pid in refs]
    return record, by_label, participants


async def apply_meeting_extraction(
    uow: UnitOfWork,
    extraction_id: UUID,
    *,
    now: datetime.datetime,
    relink: Mapping[UUID, Relink] | None = None,
) -> ApplyReport:
    """Apply one meeting extraction; ``relink`` as in ``apply_extraction`` (R2 only)."""
    await set_code_path(uow, "apply")
    await uow.session.execute(MERGE_LOCK_SQL, {"user_id": str(uow.user_id)})
    ext = await get_extraction(uow, extraction_id, for_update=True)
    if ext.apply_status != "pending" or ext.status != "succeeded" or ext.output is None:
        return ApplyReport(applied=False)
    recording = (await meetings.recordings_by_source(uow, [ext.source_item_id])).get(ext.source_item_id)
    if recording is None or recording.meeting_id is None:
        return ApplyReport(applied=False)
    view = await meetings.current_transcript(uow, recording.id)
    if view is None:
        return ApplyReport(applied=False)
    out = MeetingExtraction.model_validate(ext.output)
    meeting_id = recording.meeting_id
    _, _, participants = await _people(uow, meeting_id)
    labels = {s.speaker_label for s in view.segments if s.speaker_label}
    proposals = []
    for sp in out.speaker_mapping:
        if sp.label not in labels:
            continue
        person = await proposal_person(uow, sp.person_email, sp.person_name, participants)
        if person is not None:
            proposals.append(meetings.Proposal(sp.label, person.id, sp.confidence, "ai_proposal", "ai"))
    mapped = await meetings.record_ai_mappings(
        uow, meeting_id, proposals, extraction_id=ext.id, model=ext.model, now=now
    )
    record, by_label, participants = await _people(uow, meeting_id)
    c = _Context(
        ext=ext,
        out=out,
        meeting=record,
        segments={s.seq: s for s in view.segments},
        duration_ms=int(view.duration_s * 1000) if view.duration_s else None,
        self_p=await get_self_person(uow),
        by_label=by_label,
        participants=participants,
        tz=(await get_user_settings(uow)).timezone,
        now=now,
        relink=relink or {},
    )
    report = ApplyReport(applied=True)
    await _statements(uow, c, report)
    decided = await _decisions(uow, c, report)
    await _signals(uow, c, report, decided)
    concerns = await _concerns(uow, c)
    await meetings.store_summary(
        uow,
        meeting_id,
        summary={"text": out.summary, "topics": list(out.topics), "concerns": concerns},
        extraction_id=ext.id,
        model=ext.model,
        prompt_version=MEETING_PROMPT_VERSION,
        now=now,
    )
    mentions = []
    for mention in out.mentions:
        if mention.type != "person":
            continue
        person = await resolve_name(uow, mention.surface_text, participants)
        if person is not None:
            mentions.append(
                MentionIn(
                    ext.source_item_id,
                    "person",
                    person.id,
                    mention.surface_text,
                    0.8,
                    "extraction",
                    record.starts_at,
                )
            )
    await record_mentions(uow, mentions)
    await record_entity_event(
        uow,
        entity_type="meeting",
        entity_id=meeting_id,
        event_type="meeting_processed",
        actor="model",
        authority=2,
        materiality=2,
        occurred_at=record.ends_at,
        dedupe_key=model_dedupe_key(ext.id, 0, "meeting_processed"),
        payload={"recording_id": str(recording.id), "extraction_id": str(ext.id)},
    )
    await mark_applied(uow, ext.id, at=now)
    await meetings.mark_processed(uow, recording.id, meeting_id, mapped_labels=mapped, now=now)
    log.info(
        "meeting_extraction_applied",
        extraction_id=str(ext.id),
        created=len(report.created),
        updated=len(report.updated),
        ungrounded=report.dropped_ungrounded,
    )
    return report


# ---------------------------------------------------------------- speaker confirmation


async def remap_speaker_items(
    uow: UnitOfWork,
    meeting_id: UUID,
    *,
    before: dict[str, UUID],
    after: dict[str, UUID],
    request_key: str,
    now: datetime.datetime,
) -> list[UUID]:
    """Items whose statements came from a remapped label get owner, requester, counterparty and
    direction recomputed with the §5.5 mapping, as ``actor = user`` events (authority 5)."""
    changed = {label for label in set(before) | set(after) if before.get(label) != after.get(label)}
    if not changed:
        return []
    recording = await meetings.recording_for_meeting(uow, meeting_id)
    if recording is None or recording.source_item_id is None:
        return []
    record, by_label, participants = await _people(uow, meeting_id)
    self_p = await get_self_person(uow)
    ie = item_evidence_table
    touched: list[UUID] = []
    for ext in await succeeded_for_source(uow, recording.source_item_id, "meeting_extract"):
        if ext.apply_status != "applied" or ext.output is None:
            continue
        out = MeetingExtraction.model_validate(ext.output)
        for idx, st in enumerate(out.statements):
            if st.speaker not in changed:
                continue
            ev_id = evidence_id_for(ext.id, idx)
            item_ids = [
                r.item_id
                for r in await uow.session.execute(
                    select(ie.c.item_id).where(
                        ie.c.user_id == uow.user_id, ie.c.evidence_id == ev_id, ie.c.item_type == "work_item"
                    )
                )
            ]
            if not item_ids:
                continue
            speaker = by_label.get(st.speaker)
            owner = await resolve_ref(
                uow,
                st.owner_ref,
                speaker=speaker,
                self_p=self_p,
                by_label=by_label,
                participants=participants,
            )
            beneficiary = await resolve_ref(
                uow,
                st.beneficiary_ref,
                speaker=speaker,
                self_p=self_p,
                by_label=by_label,
                participants=participants,
            )
            mapped = map_statement(
                kind=st.statement_kind,
                speaker_id=speaker.id if speaker else None,
                self_id=self_p.id,
                owner_id=owner.id if owner else None,
                beneficiary_id=beneficiary.id if beneficiary else None,
                addressees=tuple(p.id for p in participants if speaker is None or p.id != speaker.id),
            )
            sets = {
                "direction": mapped.direction,
                "owner_person_id": mapped.owner_id,
                "requester_person_id": mapped.requester_id,
                "counterparty_person_id": mapped.counterparty_id,
            }
            for item_id in item_ids:
                result = await append_event(
                    uow,
                    item_id=item_id,
                    event_type="speaker_remapped",
                    actor="user",
                    authority=5,
                    materiality=2,
                    occurred_at=now,
                    dedupe_key=user_dedupe_key(request_key, f"speaker_remapped:{ext.id}:{idx}", item_id),
                    payload={"set": _json(sets), "speaker_label": st.speaker, "meeting_id": str(record.id)},
                )
                touched.append(result.item_id)
        touched += await _remap_signals(uow, ext, out, changed, by_label)
    return sorted(set(touched))


async def _remap_signals(
    uow: UnitOfWork,
    ext: ExtractionRecord,
    out: MeetingExtraction,
    changed: set[str],
    by_label: dict[str, PersonRef],
) -> list[UUID]:
    """Status signals of a remapped label: the model event again, with the new speaker and the
    §8.2 authority (still a model claim, never authority 5; BACKEND_DESIGN.md §16.9)."""
    ie = item_evidence_table
    touched: list[UUID] = []
    tz = (await get_user_settings(uow)).timezone
    for n, sig in enumerate(out.status_signals):
        if sig.speaker not in changed:
            continue
        target_id = _candidate(ext, sig.candidate_id, "work_item")
        if target_id is None:
            continue
        idx = SIGNAL_INDEX + n
        ev_id = evidence_id_for(ext.id, idx)
        linked = (
            await uow.session.execute(
                select(ie.c.item_id).where(
                    ie.c.user_id == uow.user_id,
                    ie.c.evidence_id == ev_id,
                    ie.c.item_type == "work_item",
                )
            )
        ).scalar_one_or_none()
        if linked is None:
            continue
        evidence = await get_evidence(uow, ev_id)
        occurred = evidence.occurred_at or datetime.datetime.now(datetime.UTC)
        event_type, sets = _signal_event(sig, evidence.quote, occurred, ev_id, reference=occurred, tz=tz)
        speaker = by_label.get(sig.speaker)
        result = await append_event(
            uow,
            item_id=linked,
            event_type=event_type,
            actor="model",
            authority=signal_authority(speaker.id if speaker else None, await _owner_of(uow, linked)),
            materiality=3 if event_type in ("due_changed", "completed_claim") else 1,
            occurred_at=occurred,
            dedupe_key=model_dedupe_key(
                ext.id, idx, f"{event_type}:speaker:{speaker.id if speaker else 'none'}"
            ),
            payload={
                "set": _json(sets),
                "by_person": str(speaker.id) if speaker else None,
                "speaker_label": sig.speaker,
                "signal": sig.signal,
                "confidence": sig.confidence,
                "speaker_remapped": True,
            },
            evidence_id=ev_id,
            extraction_id=ext.id,
        )
        touched.append(result.item_id)
    return touched
