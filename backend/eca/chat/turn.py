"""One chat turn (AI_PIPELINE.md §6.4; CONTEXT_ARCHITECTURE.md §9.1; BACKEND_DESIGN.md §16.7).

``start_turn`` (one transaction, before the response starts): load the session, claim the
``Idempotency-Key`` (a completed key replays its answer), store the question, read the session
context and the budget level. ``run_turn`` (streamed): plan (rules, then AI-05), query embedding
for discovery intents, packet assembly with its trace, routing (deterministic / AI-06 / AI-07),
grounding checks, abstention or degradation, then one transaction that stores the answer with
citation snapshots and provenance, updates the focus map, completes the key and, for "what
changed", moves the ``chat`` checkpoint. No model call happens inside a transaction. A failure
releases the key and ends the stream with an ``error`` event; nothing partial is stored.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from eca import identity, intelligence, retrieval
from eca.chat.grounding import verify
from eca.chat.render import (
    Rendered,
    citation_snapshots,
    render_abstention,
    render_degraded,
    render_list,
    render_unsupported,
    render_verified,
)
from eca.chat.sessions import (
    AssistantMessage,
    MessageView,
    add_assistant_message,
    add_user_message,
    get_message,
    get_session,
    load_state,
    merge_focus,
    touch_session,
)
from eca.intelligence import AIClient, BudgetLevel
from eca.platform.errors import DomainError
from eca.platform.idempotency import claim_key, release_key, store_response
from eca.platform.uow import UnitOfWorkFactory

log = structlog.get_logger("eca.chat")

FOCUS_KINDS = {"work_item", "decision", "person", "project", "conversation", "meeting"}
NON_FACTUAL = frozenset({"next_action"})


@dataclass(frozen=True)
class ChatEvent:
    name: str  # plan | sources | delta | final | error
    data: dict[str, Any]


@dataclass(frozen=True)
class TurnStart:
    user_id: UUID
    session_id: UUID
    question_id: UUID
    text: str
    conversation_id: UUID | None
    key: str
    state: retrieval.SessionState
    budget: BudgetLevel
    first_turn: bool


@dataclass(frozen=True)
class Replay:
    message: MessageView


async def start_turn(
    factory: UnitOfWorkFactory,
    *,
    user_id: UUID,
    session_id: UUID,
    text: str,
    conversation_id: UUID | None,
    key: str,
    digest: bytes,
    now: datetime.datetime,
) -> TurnStart | Replay:
    async with factory(user_id=user_id) as uow:
        view = await get_session(uow, session_id, for_update=True)
        stored = await claim_key(uow, key, digest, now=now)
        if stored is not None:
            return Replay(await get_message(uow, UUID(str(stored.body["message_id"]))))
        state = await load_state(uow, view, now=now)
        question_id = await add_user_message(uow, session_id, text)
        budget = await intelligence.budget_level(uow, now=now)
    return TurnStart(
        user_id,
        session_id,
        question_id,
        text,
        conversation_id,
        key,
        state,
        budget,
        first_turn=not state.turns,
    )


@dataclass
class Outcome:
    rendered: Rendered
    tier: str
    confidence: str | None
    model: str | None = None
    prompt_version: str | None = None
    call_ids: tuple[UUID, ...] = ()


async def _answer(
    client: AIClient | None, start: TurnStart, plan: retrieval.Plan, asm: retrieval.Assembly
) -> Outcome:
    intent = plan.intent
    if asm.clarification:
        return Outcome(render_abstention(asm, clarification=asm.clarification), "deterministic", None)
    if intent == "unsupported":
        return Outcome(render_unsupported(), "deterministic", None)
    tier = plan.tier
    if tier == "deterministic":
        return Outcome(render_list(asm, intent), "deterministic", "high" if asm.matched else "medium")
    if not asm.matched:  # abstention pre-check: no model call (AI_PIPELINE.md §5.7)
        return Outcome(render_abstention(asm), "abstain", "low")
    if client is None:
        return Outcome(render_degraded(asm, "The AI service is not configured.", intent), "degraded", None)
    if start.budget is BudgetLevel.HARD:
        spent = "Your daily AI budget is used up; showing the retrieved items."
        return Outcome(render_degraded(asm, spent, intent), "degraded", None)
    notice: str | None = None
    synthesis = tier == "T2"
    if synthesis and start.budget is BudgetLevel.SOFT:
        synthesis, notice = False, "Your daily AI budget is nearly used: this is a shorter answer."
    factual = intent not in NON_FACTUAL
    packet_text = asm.packet.render()
    call = await intelligence.run_answer(client, packet_text, synthesis=synthesis, user_id=start.user_id)
    calls = call.call_ids
    if call.output is None:
        return Outcome(
            render_degraded(asm, "The answer model is unavailable.", intent), "degraded", None, call_ids=calls
        )
    verified = verify(call.output, asm.packet, factual=factual)
    used = call
    if verified.abstain and not synthesis and verified.missing_info and start.budget is BudgetLevel.OK:
        escalated = await intelligence.run_answer(client, packet_text, synthesis=True, user_id=start.user_id)
        calls = calls + escalated.call_ids
        if escalated.output is not None:
            again = verify(escalated.output, asm.packet, factual=factual)
            if not again.abstain:
                verified, used = again, escalated
    if verified.abstain:
        return Outcome(
            render_abstention(asm, missing_info=verified.missing_info),
            "abstain",
            "low",
            used.model,
            used.prompt_version,
            calls,
        )
    tier_used = "T2" if used.role == "answer_synthesis" else "T1"
    return Outcome(
        render_verified(asm, verified, notice=notice),
        tier_used,
        verified.confidence,
        used.model,
        used.prompt_version,
        calls,
    )


def _cited_focus(asm: retrieval.Assembly, cited: list[str]) -> list[retrieval.FocusEntry]:
    by_cid = asm.packet.by_cid()
    out = list(asm.focus)
    for cid in cited:
        item = by_cid.get(cid)
        if item is not None and item.kind in FOCUS_KINDS and item.entity_id is not None:
            out.append(retrieval.FocusEntry(item.kind, item.entity_id, item.line, 0))
    return out


def _provenance(
    asm: retrieval.Assembly, outcome: Outcome, cited: list[str], now: datetime.datetime
) -> dict[str, Any]:
    """AI_PIPELINE.md §5.8 answer provenance: a band, not a number."""
    by_cid = asm.packet.by_cid()
    items = [by_cid[c] for c in cited if c in by_cid]
    return {
        "source": sorted({str(s) for i in items for s in i.source_item_ids}),
        "evidence": sorted({str(e) for i in items for e in i.evidence_ids}),
        "confidence_band": outcome.confidence,
        "derived_at": now.isoformat(),
        "extraction_method": "llm" if outcome.model else "deterministic",
        "model": outcome.model,
        "prompt_version": outcome.prompt_version,
        "ai_call_ids": [str(c) for c in outcome.call_ids],
        "planner": asm.plan.planner,
        "scenario": asm.plan.scenario,
    }


def message_payload(msg: MessageView) -> dict[str, Any]:
    return {
        "id": str(msg.id),
        "session_id": str(msg.session_id),
        "role": msg.role,
        "content": msg.content,
        "reply_to_id": str(msg.reply_to_id) if msg.reply_to_id else None,
        "scenario": msg.scenario,
        "answer_tier": msg.answer_tier,
        "claims": msg.claims,
        "citations": msg.citations,
        "confidence": msg.confidence,
        "provenance": msg.provenance,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


def replay_events(replay: Replay) -> list[ChatEvent]:
    msg = replay.message
    return [
        ChatEvent("sources", {"citations": msg.citations}),
        ChatEvent("delta", {"text": msg.content}),
        ChatEvent("final", {"message": message_payload(msg)}),
    ]


async def run_turn(
    factory: UnitOfWorkFactory, client: AIClient | None, start: TurnStart, *, now: datetime.datetime
) -> AsyncIterator[ChatEvent]:
    try:
        allow_ai = client is not None and start.budget is not BudgetLevel.HARD
        plan, plan_calls = await retrieval.plan_question(
            start.text,
            conversation_id=start.conversation_id,
            client=client,
            allow_ai=allow_ai,
            user_id=start.user_id,
            meeting_id=start.state.scope.meeting_id if start.state.scope.kind == "meeting" else None,
        )
        yield ChatEvent("plan", {"scenario": plan.scenario, "tier": plan.tier, "planner": plan.planner})
        vector, model = None, None
        if allow_ai and retrieval.needs_discovery(plan):
            vector, model = await retrieval.embed_question(client, start.text, user_id=start.user_id)
        async with factory(user_id=start.user_id) as uow:
            asm = await retrieval.assemble(
                uow,
                plan=plan,
                question=start.text,
                session=start.state,
                now=now,
                query_vector=vector,
                embedding_model=model,
            )
        yield ChatEvent(
            "sources",
            {
                "citations": [
                    {
                        "cid": i.cid,
                        "kind": i.kind,
                        "text": i.line,
                        "data_class": i.data_class,
                        "source_item_ids": [str(s) for s in i.source_item_ids],
                    }
                    for i in asm.packet.items
                ],
                "coverage": asm.coverage.text,
            },
        )
        outcome = await _answer(client, start, plan, asm)
        outcome.call_ids = tuple(plan_calls) + outcome.call_ids
        yield ChatEvent("delta", {"text": outcome.rendered.text})
        cited = outcome.rendered.cited
        async with factory(user_id=start.user_id) as uow:
            message_id = await add_assistant_message(
                uow,
                start.session_id,
                AssistantMessage(
                    content=outcome.rendered.text,
                    reply_to_id=start.question_id,
                    scenario=plan.scenario,
                    answer_tier=outcome.tier,
                    claims=outcome.rendered.claims,
                    citations=citation_snapshots(asm, cited),
                    confidence=outcome.confidence,
                    provenance=_provenance(asm, outcome, cited, now),
                    retrieval_trace_id=asm.trace_id,
                    ai_call_ids=list(outcome.call_ids),
                ),
            )
            focus = merge_focus(start.state.focus, _cited_focus(asm, cited), turn=len(start.state.turns) + 1)
            await touch_session(
                uow, start.session_id, focus=focus, title=start.text if start.first_turn else None, now=now
            )
            await store_response(uow, start.key, status_code=200, body={"message_id": str(message_id)})
            if plan.intent == "what_changed":
                await identity.mark_seen(uow, "chat", at=now)
            stored = await get_message(uow, message_id)
        yield ChatEvent("final", {"message": message_payload(stored)})
        log.info(
            "chat_answered",
            scenario=plan.scenario,
            tier=outcome.tier,
            planner=plan.planner,
            calls=len(outcome.call_ids),
        )
    except Exception as exc:
        log.error("chat_turn_failed", error_type=type(exc).__name__)
        try:
            async with factory(user_id=start.user_id) as uow:
                await release_key(uow, start.key)
        except Exception as release_exc:  # the stream must still end with an error event
            log.error("chat_key_release_failed", error_type=type(release_exc).__name__)
        code = exc.code if isinstance(exc, DomainError) else "internal_error"
        yield ChatEvent("error", {"code": code, "title": "The answer could not be completed. Please retry."})
