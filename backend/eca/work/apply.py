"""Apply a stored extraction (BACKEND_DESIGN.md §8.2). Deterministic; never calls a model.

One transaction, under ``pg_advisory_xact_lock(hashtextextended('merge:' || user_id, 0))``:
grounding (§5.4 of AI_PIPELINE.md), date resolution (no invented dates), the statement mapping
(§5.5), penalty confidence (§5.3), candidate resolution and dedupe/merge (TECHNICAL_DESIGN.md
§13.6), deterministic evidence IDs, ``context_events`` through ``append_event``, provisional items
with ``AdjudicationNeeded``, the ``messages.triage`` projection and entity mentions. A retry finds
``apply_status = applied`` and does nothing (RT-02).
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from eca.communication import MessageView, get_message_view, set_triage_projection, split_forwarded
from eca.identity import get_user_settings
from eca.ingestion import set_stage
from eca.intelligence import (
    EmailExtraction,
    ExtractionRecord,
    get_extraction,
    mark_applied,
    set_code_path,
)
from eca.people import (
    MentionIn,
    PersonRef,
    find_by_email,
    get_self_person,
    record_mentions,
    resolve_name,
)
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork
from eca.work import confidence as conf
from eca.work.candidates import OpenItem, match_items, matchable_items
from eca.work.dates import DueResolution, guess_disagrees, resolve_due
from eca.work.events import ADJUDICATION_NEEDED, AdjudicationNeeded
from eca.work.mapping import authority, map_statement
from eca.work.models import decisions_table
from eca.work.service import (
    add_evidence,
    append_event,
    create_item,
    evidence_id_for,
    link_evidence,
    model_dedupe_key,
)

log = structlog.get_logger("eca.work.apply")

MERGE_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended('merge:' || :user_id, 0))")
SIGNAL_INDEX = 100
DECISION_INDEX = 200
_FORWARD_FROM = re.compile(r"^From:\s*(?:.*<)?([^<>\s]+@[^<>\s]+)>?", re.MULTILINE)
REPORTED = {
    "progress": "in progress",
    "completed_claim": "claims done",
    "delay": "delayed",
    "cancelled": "withdrawn",
    "resolved": "resolved",
    "declined": "declined",
}


@dataclass
class ApplyReport:
    applied: bool
    created: list[UUID] = field(default_factory=list)
    updated: list[UUID] = field(default_factory=list)
    dropped_ungrounded: int = 0
    dropped_low_confidence: int = 0
    dropped_rejected: int = 0
    provisional: list[UUID] = field(default_factory=list)


@dataclass(frozen=True)
class _Grounded:
    quote: str
    start: int | None
    fuzzy: bool


def _ground(quote: str, body: str) -> _Grounded | None:
    """Exact verbatim match, else whitespace/case-insensitive match (fuzzy), else None."""
    idx = body.find(quote)
    if idx >= 0:
        return _Grounded(quote, idx, False)
    norm_body = " ".join(body.split()).lower()
    norm_quote = " ".join(quote.split()).lower()
    if norm_quote and norm_quote in norm_body:
        return _Grounded(quote, None, True)
    return None


async def _resolve_ref(
    uow: UnitOfWork,
    ref: str | None,
    *,
    speaker: PersonRef | None,
    self_p: PersonRef,
    participants: list[PersonRef],
) -> PersonRef | None:
    if ref is None or ref == "unknown":
        return None
    if ref in ("speaker", "sender"):
        return speaker
    if ref == "self":
        return self_p
    if ref.startswith("recipient:"):
        email = ref.split(":", 1)[1].strip().lower()
        for p in participants:
            if (p.primary_email or "").lower() == email:
                return p
        return await find_by_email(uow, email)
    if ref.startswith("name:"):
        return await resolve_name(uow, ref.split(":", 1)[1], participants)
    return None


async def _forwarded_author(uow: UnitOfWork, view: MessageView, ref: str | None) -> PersonRef | None:
    if ref and "@" in ref:
        found = await find_by_email(uow, ref.strip("<> "))
        if found:
            return found
    _, forwarded = split_forwarded(view.body_clean)
    if forwarded and (m := _FORWARD_FROM.search(forwarded)):
        return await find_by_email(uow, m.group(1))
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


async def apply_extraction(uow: UnitOfWork, extraction_id: UUID, *, now: datetime.datetime) -> ApplyReport:
    await set_code_path(uow, "apply")
    await uow.session.execute(MERGE_LOCK_SQL, {"user_id": str(uow.user_id)})
    ext = await get_extraction(uow, extraction_id, for_update=True)
    if ext.apply_status != "pending" or ext.status != "succeeded" or ext.output is None:
        return ApplyReport(applied=False)
    view = await get_message_view(uow, ext.source_item_id)
    self_p = await get_self_person(uow)
    tz = (await get_user_settings(uow)).timezone
    out = EmailExtraction.model_validate(ext.output)
    report = ApplyReport(applied=True)
    participants = [view.sender, *view.to, *view.cc]
    addressees = tuple(p.id for p in (*view.to, *view.cc))
    items = await matchable_items(uow, now=view.sent_at)
    touched: set[UUID] = set()

    for idx, st in enumerate(out.statements):
        grounded = _ground(st.evidence_quote, view.body_clean)
        if grounded is None:
            report.dropped_ungrounded += 1
            continue
        _, forwarded_block = split_forwarded(view.body_clean)
        in_forward = st.in_forwarded_content or bool(forwarded_block and st.evidence_quote in forwarded_block)
        speaker: PersonRef | None = view.sender
        if in_forward:
            speaker = await _forwarded_author(uow, view, st.forwarded_author_ref)
        owner = await _resolve_ref(
            uow, st.owner_ref, speaker=speaker, self_p=self_p, participants=participants
        )
        beneficiary = await _resolve_ref(
            uow, st.beneficiary_ref, speaker=speaker, self_p=self_p, participants=participants
        )
        mapped = map_statement(
            kind=st.statement_kind,
            speaker_id=speaker.id if speaker else None,
            self_id=self_p.id,
            owner_id=owner.id if owner else None,
            beneficiary_id=beneficiary.id if beneficiary else None,
            addressees=addressees if not in_forward else (),
            forwarded=in_forward,
        )
        due_text = st.due_text if st.due_text and st.due_text in grounded.quote else None
        due = resolve_due(due_text, reference=view.sent_at, timezone=tz)
        penalties = []
        if grounded.fuzzy:
            penalties.append("fuzzy_grounding")
        if mapped.owner_id is None:
            penalties.append("owner_unresolved")
        if guess_disagrees(due, st.due_iso_guess):
            penalties.append("date_disagreement")
        if st.statement_kind.startswith("report_"):
            penalties.append("report_statement")
        if in_forward:
            penalties.append("forwarded")
        if mapped.owner_inconsistent:
            penalties.append("owner_inconsistent")
        c = conf.compute(st.confidence, penalties)
        ev_id = evidence_id_for(ext.id, idx)
        auth = authority(
            speaker_id=speaker.id if speaker else None, owner_id=mapped.owner_id, strength=mapped.strength
        )
        target_id = _candidate(ext, st.candidate_id)

        if st.statement_kind == "acceptance" and target_id is None:
            # An acceptance attaches to the open request it answers (same thread or participants).
            target_match, _ = match_items(
                [i for i in items if i.type == "request"],
                type_="request",
                title=st.action,
                owner_id=mapped.owner_id,
                counterparty_id=None,
                due_at=None,
            )
            target_id = target_match.item.id if target_match else _single_open_request(items, mapped.owner_id)
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
        else:
            ambiguous = []
        target = next((i for i in items if i.id == target_id), None)
        # A rejected item is re-suggested only by a new explicit statement from its owner (§12.3).
        owner_restates = (
            mapped.strength == "explicit"
            and speaker is not None
            and speaker.id == (target.owner_person_id if target else None)
        )
        if target is not None and target.verification_status == "rejected" and not owner_restates:
            report.dropped_rejected += 1
            continue
        if c.value < conf.MIN_TO_CREATE and target is None:
            report.dropped_low_confidence += 1
            continue

        await add_evidence(
            uow,
            evidence_id=ev_id,
            source_item_id=view.source_item_id,
            extraction_id=ext.id,
            index=idx,
            quote=grounded.quote,
            char_start=grounded.start,
            occurred_at=view.sent_at,
        )
        fields: dict[str, Any] = {
            "type": mapped.type,
            "title": st.action,
            "direction": mapped.direction,
            "owner_person_id": mapped.owner_id,
            "counterparty_person_id": mapped.counterparty_id,
            "requester_person_id": mapped.requester_id,
            "commitment_strength": mapped.strength,
            "statement_kind": st.statement_kind,
            "confidence": c.value,
            "confidence_band": c.band,
            "project_hint": out.project_hint,
            **_due_fields(due),
        }
        if target is not None:
            sets: dict[str, Any] = {}
            event_type = "restated"
            if st.statement_kind == "acceptance":
                event_type = "accepted"
                sets = {
                    "type": "commitment",
                    "statement_kind": "acceptance",
                    "direction": "my_commitment" if speaker and speaker.id == self_p.id else "waiting_for",
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
                occurred_at=view.sent_at,
                dedupe_key=model_dedupe_key(ext.id, idx, event_type),
                payload={
                    "set": _json(sets),
                    "by_person": str(speaker.id) if speaker else None,
                    "confidence": c.value,
                    "penalties": list(c.penalties),
                },
                evidence_id=ev_id,
                extraction_id=ext.id,
            )
            await link_evidence(uow, "work_item", target.id, ev_id, "updates" if sets else "supports")
            report.updated.append(target.id)
            touched.add(target.id)
            continue
        provisional = bool(ambiguous)
        if provisional:
            fields["confidence"] = min(c.value, 0.59)
            fields["confidence_band"] = "low"
        item_id = await create_item(
            uow,
            fields=fields,
            origin="ai",
            extraction_id=ext.id,
            method="llm",
            model=ext.model,
            derived_at=now,
            actor="model",
            authority=auth,
            materiality=2,
            occurred_at=view.sent_at,
            dedupe_key=model_dedupe_key(ext.id, idx, "created"),
            evidence_id=ev_id,
            pending_adjudication=provisional,
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
                view.sent_at,
            )
        )
        if provisional:
            report.provisional.append(item_id)
            await publish(
                uow,
                NewEvent(
                    ADJUDICATION_NEEDED,
                    "work_item",
                    item_id,
                    AdjudicationNeeded(work_item_id=item_id, extraction_id=ext.id, statement_index=idx),
                ),
            )

    for n, sig in enumerate(out.status_signals):
        target_id = _candidate(ext, sig.candidate_id)
        grounded = _ground(sig.evidence_quote, view.body_clean)
        if target_id is None or grounded is None:
            report.dropped_ungrounded += 1
            continue
        target = next((i for i in items if i.id == target_id), None)
        idx = SIGNAL_INDEX + n
        ev_id = evidence_id_for(ext.id, idx)
        await add_evidence(
            uow,
            evidence_id=ev_id,
            source_item_id=view.source_item_id,
            extraction_id=ext.id,
            index=idx,
            quote=grounded.quote,
            char_start=grounded.start,
            occurred_at=view.sent_at,
        )
        speaker_id = view.sender.id
        owner_id = target.owner_person_id if target else None
        auth = 4 if speaker_id == owner_id else 3
        sets = {}
        event_type = "status_signal"
        if sig.signal == "new_deadline":
            due_text = sig.new_due_text if sig.new_due_text and sig.new_due_text in grounded.quote else None
            sets = _due_fields(resolve_due(due_text, reference=view.sent_at, timezone=tz))
            event_type = "due_changed"
        elif sig.signal == "accepted":
            sets = {
                "type": "commitment",
                "statement_kind": "acceptance",
                "commitment_strength": "explicit",
                "direction": "my_commitment" if speaker_id == self_p.id else "waiting_for",
            }
            event_type = "accepted"
        elif sig.signal == "completed_claim":
            sets = {
                "reported_status": REPORTED[sig.signal],
                "reported_status_at": view.sent_at,
                "reported_status_evidence_id": ev_id,
            }
            event_type = "completed_claim"
        elif sig.signal in REPORTED:
            sets = {
                "reported_status": REPORTED[sig.signal],
                "reported_status_at": view.sent_at,
                "reported_status_evidence_id": ev_id,
            }
            if sig.signal == "cancelled":
                sets["lifecycle_status"] = "cancelled"  # the fold turns this into a prompt, never a fact
        await append_event(
            uow,
            item_id=target_id,
            event_type=event_type,
            actor="model",
            authority=auth,
            materiality=3 if event_type in ("due_changed", "completed_claim") else 1,
            occurred_at=view.sent_at,
            dedupe_key=model_dedupe_key(ext.id, idx, event_type),
            payload={
                "set": _json(sets),
                "by_person": str(speaker_id),
                "signal": sig.signal,
                "confidence": sig.confidence,
            },
            evidence_id=ev_id,
            extraction_id=ext.id,
        )
        await link_evidence(
            uow, "work_item", target_id, ev_id, "completes" if sig.signal == "completed_claim" else "updates"
        )
        report.updated.append(target_id)

    for n, dec in enumerate(out.decisions):
        grounded = _ground(dec.evidence_quote, view.body_clean)
        if grounded is None:
            report.dropped_ungrounded += 1
            continue
        idx = DECISION_INDEX + n
        ev_id = evidence_id_for(ext.id, idx)
        await add_evidence(
            uow,
            evidence_id=ev_id,
            source_item_id=view.source_item_id,
            extraction_id=ext.id,
            index=idx,
            quote=grounded.quote,
            char_start=grounded.start,
            occurred_at=view.sent_at,
        )
        c = conf.compute(dec.confidence, ["fuzzy_grounding"] if grounded.fuzzy else [])
        decision_id = uuid7()
        await uow.session.execute(
            insert(decisions_table).values(
                id=decision_id,
                user_id=uow.user_id,
                kind=dec.kind,
                statement=dec.statement,
                decided_at=view.sent_at if dec.kind == "decision" else None,
                conversation_id=view.conversation_id,
                project_hint=out.project_hint,
                origin="ai",
                verification_status="suggested",
                confidence=c.value,
                confidence_band=c.band,
                extraction_id=ext.id,
                extraction_method="llm",
                model=ext.model,
                derived_at=now,
                user_fields=[],
                version=1,
            )
        )
        await link_evidence(uow, "decision", decision_id, ev_id, "supports")

    triage = {**out.triage.model_dump(mode="json"), "gist": out.gist}
    await set_triage_projection(uow, message_id=view.message_id, triage=triage, extraction_id=ext.id)
    mentions = []
    for mention in out.mentions:
        if mention.type != "person":
            continue
        person = await resolve_name(uow, mention.surface_text, participants)
        if person is not None:
            mentions.append(
                MentionIn(
                    view.source_item_id,
                    "person",
                    person.id,
                    mention.surface_text,
                    0.8,
                    "extraction",
                    view.sent_at,
                )
            )
    await record_mentions(uow, mentions)
    await mark_applied(uow, ext.id, at=now)
    await set_stage(uow, view.source_item_id, expected=("extracted",), new="applied")
    log.info(
        "extraction_applied",
        extraction_id=str(ext.id),
        created=len(report.created),
        updated=len(report.updated),
        ungrounded=report.dropped_ungrounded,
    )
    return report


def _candidate(ext: ExtractionRecord, code: str | None) -> UUID | None:
    if not code:
        return None
    entry = ext.candidate_map.get(code)
    if not entry or entry.get("entity_type") != "work_item":
        return None
    return UUID(entry["entity_id"])  # merged items redirect inside append_event (§12.5)


def _single_open_request(items: list[OpenItem], owner_id: UUID | None) -> UUID | None:
    open_requests = [
        i
        for i in items
        if i.type == "request"
        and i.owner_person_id == owner_id
        and i.lifecycle_status in ("open", "in_progress")
        and i.verification_status != "rejected"
    ]
    return open_requests[-1].id if len(open_requests) == 1 else None


def _json(sets: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in sets.items():
        out[k] = v.isoformat() if isinstance(v, datetime.datetime) else (str(v) if isinstance(v, UUID) else v)
    return out
