"""Reply guidance (slice 3.3; PRD §29; AI_PIPELINE.md §4.2 O13, §5.7, §5.9; BACKEND_DESIGN.md §16.8).

``start_guidance`` (one transaction, before the stream): claim the ``Idempotency-Key`` (a stored
response replays), check the thread and read the budget level. ``run_guidance`` (streamed):
assemble the RG packet (no model call), AI-08 outside any transaction, grounding checks on every
section's claims, the draft labelled as a suggestion with warnings for details found in no packet
item, then store the result as the key's response. The draft is never sent and never written to
Gmail: no send, draft or calendar action exists. A failure releases the key; nothing is logged
but IDs.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from eca import intelligence, retrieval
from eca.chat.grounding import Verified, checked_tokens, tokens, verify
from eca.chat.render import ABSTAIN, DEGRADED, citation_snapshots
from eca.chat.turn import ChatEvent
from eca.intelligence import AIClient, Answer, AnswerClaim, BudgetLevel
from eca.platform.errors import DomainError
from eca.platform.idempotency import claim_key, release_key, store_response
from eca.platform.uow import UnitOfWorkFactory

log = structlog.get_logger("eca.chat.reply")

DEFAULT_REQUEST = "Help me reply to the latest message in this thread."
DRAFT_LABEL = "Suggestion — draft for copying; review before sending. It is never sent for you."
SECTIONS = ("context", "previous_agreement", "current_status")


@dataclass(frozen=True)
class GuidanceStart:
    user_id: UUID
    conversation_id: UUID
    recipient_id: UUID | None
    instructions: str | None
    key: str
    budget: BudgetLevel


@dataclass(frozen=True)
class GuidanceReplay:
    body: dict[str, Any]


async def start_guidance(
    factory: UnitOfWorkFactory,
    *,
    user_id: UUID,
    conversation_id: UUID,
    instructions: str | None,
    key: str,
    digest: bytes,
    now: datetime.datetime,
) -> GuidanceStart | GuidanceReplay:
    async with factory(user_id=user_id) as uow:
        recipient = await retrieval.reply_anchor(uow, conversation_id)  # 404 for unknown threads
        stored = await claim_key(uow, key, digest, now=now)
        if stored is not None:
            return GuidanceReplay(stored.body)
        budget = await intelligence.budget_level(uow, now=now)
    return GuidanceStart(user_id, conversation_id, recipient, instructions, key, budget)


def _claims_json(verified: Verified) -> list[dict[str, Any]]:
    return [
        {
            "text": c.text,
            "citations": list(c.citations),
            "kind": c.kind,
            "flagged": c.flagged,
            "reason": c.reason,
        }
        for c in verified.claims
    ]


def draft_warnings(draft: str, asm: retrieval.Assembly, instructions: str | None) -> list[str]:
    """Numbers, dates and names in the draft that no packet item (or the user's own instructions)
    contains: shown to the user as "not found in the sources" (AI_PIPELINE.md §5.9)."""
    known = set().union(*(tokens(i.text) for i in asm.packet.items), tokens(asm.packet.frame))
    if instructions:
        known |= tokens(instructions)
    return sorted(checked_tokens(draft) - known)


def _deterministic_context(asm: retrieval.Assembly) -> list[dict[str, Any]]:
    """The degraded route (§14): the retrieved thread state as plain lines, no draft."""
    return [
        {"text": i.line, "citations": [i.cid], "kind": i.claim_kind, "flagged": False, "reason": None}
        for i in asm.packet.items
        if i.cid and i.kind in ("conversation", "work_item", "decision", "summary", "gist_timeline")
    ][:10]


def _body(
    start: GuidanceStart,
    asm: retrieval.Assembly,
    *,
    tier: str,
    sections: dict[str, list[dict[str, Any]]],
    draft: str | None,
    warnings: list[str],
    confidence: str | None,
    notice: str | None,
    missing_info: str | None,
    call: intelligence.InteractiveCall[Any] | None,
    now: datetime.datetime,
) -> dict[str, Any]:
    cited = sorted({c for claims in sections.values() for claim in claims for c in claim["citations"]})
    return {
        "conversation_id": str(start.conversation_id),
        "tier": tier,
        "notice": notice,
        "sections": sections,
        "draft": {"text": draft, "kind": "recommendation", "label": DRAFT_LABEL, "warnings": warnings}
        if draft
        else None,
        "confidence": confidence,
        "missing_info": missing_info,
        "citations": citation_snapshots(asm, cited),
        "coverage": asm.coverage.text,
        "provenance": {
            "extraction_method": "llm" if call and call.model else "deterministic",
            "model": call.model if call else None,
            "prompt_version": call.prompt_version if call else None,
            "ai_call_ids": [str(c) for c in call.call_ids] if call else [],
            "derived_at": now.isoformat(),
            "retrieval_trace_id": str(asm.trace_id),
        },
    }


async def _guidance(
    client: AIClient | None, start: GuidanceStart, asm: retrieval.Assembly, now: datetime.datetime
) -> dict[str, Any]:
    call: intelligence.InteractiveCall[Any] | None = None

    def respond(
        tier: str,
        sections: dict[str, list[dict[str, Any]]],
        *,
        confidence: str | None,
        notice: str | None = None,
        draft: str | None = None,
        missing_info: str | None = None,
    ) -> dict[str, Any]:
        full = {name: sections.get(name, []) for name in SECTIONS}
        warnings = draft_warnings(draft, asm, start.instructions) if draft else []
        return _body(
            start,
            asm,
            tier=tier,
            sections=full,
            draft=draft,
            warnings=warnings,
            confidence=confidence,
            notice=notice,
            missing_info=missing_info,
            call=call,
            now=now,
        )

    abstention = {
        "context": [
            {"text": ABSTAIN, "citations": ["COVERAGE"], "kind": "absence", "flagged": False, "reason": None}
        ]
    }
    if not asm.matched:  # abstention pre-check: no model call
        return respond("abstain", abstention, confidence="low")
    if client is None or start.budget is BudgetLevel.HARD:
        reason = "The AI service is not configured." if client is None else "Your daily AI budget is used up."
        return respond(
            "degraded",
            {"context": _deterministic_context(asm)},
            confidence=None,
            notice=f"{DEGRADED} {reason}",
        )
    call = await intelligence.run_reply_guidance(client, asm.packet.render(), user_id=start.user_id)
    if call.output is None:
        notice = f"{DEGRADED} The reply model is unavailable."
        return respond("degraded", {"context": _deterministic_context(asm)}, confidence=None, notice=notice)
    out = call.output
    verified: dict[str, Verified] = {}
    for name in SECTIONS:
        claims: list[AnswerClaim] = getattr(out, name)
        answer = Answer(
            answerable=True, claims=claims, confidence=out.confidence, missing_info=out.missing_info
        )
        verified[name] = verify(answer, asm.packet, factual=False)
    grounded = any(c.kind in ("source", "user", "absence") for v in verified.values() for c in v.claims)
    if not out.answerable or not grounded:
        return respond("abstain", abstention, confidence="low", missing_info=out.missing_info)
    relabelled = sum(v.relabelled for v in verified.values())
    confidence = "medium" if relabelled and out.confidence == "high" else out.confidence
    return respond(
        "T2",
        {name: _claims_json(v) for name, v in verified.items()},
        confidence=confidence,
        draft=(out.draft or "").strip() or None,
        missing_info=out.missing_info,
    )


async def run_guidance(
    factory: UnitOfWorkFactory, client: AIClient | None, start: GuidanceStart, *, now: datetime.datetime
) -> AsyncIterator[ChatEvent]:
    try:
        plan = retrieval.Plan(
            intent="reply_guidance",
            planner="fixed",
            conversation_id=start.conversation_id,
            person_ids=(start.recipient_id,) if start.recipient_id else (),
        )
        question = start.instructions or DEFAULT_REQUEST  # delimited by the packet layout
        async with factory(user_id=start.user_id) as uow:
            asm = await retrieval.assemble(
                uow, plan=plan, question=question, session=retrieval.SessionState(), now=now, surface="api"
            )
        yield ChatEvent(
            "sources",
            {
                "citations": [
                    {"cid": i.cid, "kind": i.kind, "text": i.line, "data_class": i.data_class,
                     "source_item_ids": [str(s) for s in i.source_item_ids]}
                    for i in asm.packet.items
                ],
                "coverage": asm.coverage.text,
            },
        )  # fmt: skip
        guidance = await _guidance(client, start, asm, now)
        async with factory(user_id=start.user_id) as uow:
            await store_response(uow, start.key, status_code=200, body=guidance)
        yield ChatEvent("final", {"guidance": guidance})
        log.info("reply_guidance", tier=guidance["tier"], calls=len(guidance["provenance"]["ai_call_ids"]))
    except Exception as exc:
        log.error("reply_guidance_failed", error_type=type(exc).__name__)
        try:
            async with factory(user_id=start.user_id) as uow:
                await release_key(uow, start.key)
        except Exception as release_exc:  # the stream must still end with an error event
            log.error("reply_guidance_key_release_failed", error_type=type(release_exc).__name__)
        code = exc.code if isinstance(exc, DomainError) else "internal_error"
        yield ChatEvent(
            "error", {"code": code, "title": "Reply guidance could not be completed. Please retry."}
        )


def guidance_replay_events(replay: GuidanceReplay) -> list[ChatEvent]:
    return [ChatEvent("final", {"guidance": replay.body})]
