"""Email extract and apply orchestration (BACKEND_DESIGN.md §5.6, §8.1-§8.3).

``extract`` runs in natural-key mode (§7.4): transaction 1 claims the extraction row and builds
the input; the model is called outside any transaction; transaction 2 stores the result, moves
the source stage and publishes ``ExtractionCompleted``. A succeeded row is reused without an AI
call (RT-02b at most one extra call after a crash between the call and the store).
``apply`` is a consumption handler: one transaction under the per-user merge lock.

At the hard budget cap AI-01 runs only for the user's outbound mail and VIP senders
(``importance_user`` at or above the configured threshold); other messages are deferred until the
spend window resets, without counting an attempt (AI_COST_MODEL.md §7.2).
"""

from __future__ import annotations

import datetime
from uuid import UUID

import structlog

from eca.communication import MESSAGE_NORMALIZED, MessageNormalized, get_message_view
from eca.ingestion import (
    SOURCE_ITEM_DELETED,
    SOURCE_ITEM_STAGE_DUE,
    SourceItemDeleted,
    SourceItemStageDue,
    defer_stage,
    get_source_item,
    set_stage,
    set_stage_error,
)
from eca.intelligence import (
    EXTRACTION_COMPLETED,
    AIClient,
    Claim,
    EmailInput,
    ExtractionCompleted,
    Participant,
    claim,
    default_budget_config,
    ensure_completed_event,
    get_extraction,
    latest_pending_for_source,
    run_adjudication,
    run_email_extract,
    set_code_path,
    store_failure,
    store_success,
)
from eca.people import get_self_person, importance_of
from eca.platform.clock import Clock
from eca.platform.events import HandlerContext, handles
from eca.platform.uow import UnitOfWorkFactory
from eca.work.apply import apply_extraction
from eca.work.candidates import build_candidates
from eca.work.events import ADJUDICATION_NEEDED, AdjudicationNeeded
from eca.work.meeting_apply import apply_meeting_extraction
from eca.work.purge import on_source_deleted

log = structlog.get_logger("eca.work.pipeline")

EXTRACT_HANDLER = "work.extract"
EXTRACT_DUE_HANDLER = "work.extract_due"
APPLY_HANDLER = "work.apply"
APPLY_DUE_HANDLER = "work.apply_due"
ADJUDICATE_HANDLER = "work.adjudicate"


class ExtractionRetry(RuntimeError):
    """Transient model failure below the attempt cap: the job is retried with backoff."""


async def _build_input(uow, source_item_id: UUID) -> EmailInput:  # type: ignore[no-untyped-def]
    item = await get_source_item(uow, source_item_id)
    view = await get_message_view(uow, source_item_id)
    self_p = await get_self_person(uow)
    participants = [
        Participant("sender", view.sender.primary_email or "", view.sender.display_name, view.sender.is_self)
    ]
    participants += [Participant("to", p.primary_email or "", p.display_name, p.is_self) for p in view.to]
    participants += [Participant("cc", p.primary_email or "", p.display_name, p.is_self) for p in view.cc]
    candidates = await build_candidates(uow, view, self_id=self_p.id)
    return EmailInput(
        source_item_id=source_item_id,
        content_hash=item.content_hash,
        occurred_at=view.sent_at,
        user_name=self_p.display_name or "the user",
        subject=view.subject,
        body=view.body_clean,
        participants=tuple(participants),
        candidates=tuple(candidates),
    )


async def _budget_exempt(uow, source_item_id: UUID) -> bool:  # type: ignore[no-untyped-def]
    """Outbound mail by the user, or a sender rated VIP (AI_COST_MODEL.md §7.2)."""
    view = await get_message_view(uow, source_item_id)
    if view.direction == "outbound":
        return True
    rated = (await importance_of(uow, [view.sender.id])).get(view.sender.id) or {}
    return int(rated.get("importance_user") or 0) >= default_budget_config().vip_min_importance


async def extract_source_item(
    uow_factory: UnitOfWorkFactory, client: AIClient, *, user_id: UUID, source_item_id: UUID
) -> str:
    """Returns the outcome: ``skipped`` | ``reused`` | ``succeeded`` | ``deferred`` |
    ``failed_permanent``."""
    async with uow_factory(user_id=user_id) as uow:
        await set_code_path(uow, "extract")
        item = await get_source_item(uow, source_item_id, for_update=True)
        if item.stage != "extract_pending":
            return "skipped"
        inp = await _build_input(uow, source_item_id)
        exempt = await _budget_exempt(uow, source_item_id)
        claimed: Claim = await claim(uow, inp)
        if claimed.status == "succeeded":
            await ensure_completed_event(uow, claimed.extraction_id, source_item_id, "email_extract")
            await set_stage(uow, source_item_id, expected=("extract_pending",), new="extracted")
            return "reused"
        if claimed.status == "failed_permanent":
            await set_stage(
                uow,
                source_item_id,
                expected=("extract_pending",),
                new="needs_attention",
                error_code="extraction_failed_permanent",
            )
            return "failed_permanent"
    outcome = await run_email_extract(
        client, inp, attempts_used=claimed.attempts, user_id=user_id, budget_exempt=exempt
    )
    async with uow_factory(user_id=user_id) as uow:
        await set_code_path(uow, "extract")
        if outcome.deferred_for_s is not None:
            until = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=outcome.deferred_for_s)
            await defer_stage(uow, source_item_id, until=until, error_code="budget_deferred")
            log.info(
                "extraction_deferred", source_item_id=str(source_item_id), seconds=outcome.deferred_for_s
            )
            return "deferred"
        if outcome.ok:
            await store_success(uow, claimed, outcome)
            await set_stage(uow, source_item_id, expected=("extract_pending",), new="extracted")
            return "succeeded"
        status = await store_failure(uow, claimed, outcome)
        if status == "failed_permanent":
            await set_stage(
                uow,
                source_item_id,
                expected=("extract_pending",),
                new="needs_attention",
                error_code=outcome.error_code,
            )
            return "failed_permanent"
        await set_stage_error(uow, source_item_id, error_code=outcome.error_code or "extract_retry")
    raise ExtractionRetry(outcome.error_code or "retryable model failure")


@handles(MESSAGE_NORMALIZED, name=EXTRACT_HANDLER, queue="extract", mode="natural_key")
async def on_message_normalized(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, MessageNormalized) and ctx.envelope.user_id is not None
    await extract_source_item(
        ctx.factory,
        ctx.resources.get(AIClient),
        user_id=ctx.envelope.user_id,
        source_item_id=payload.source_item_id,
    )


@handles(SOURCE_ITEM_STAGE_DUE, name=EXTRACT_DUE_HANDLER, queue="extract", mode="natural_key")
async def on_extract_due(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemStageDue) and ctx.envelope.user_id is not None
    if payload.stage == "extract_pending":
        await extract_source_item(
            ctx.factory,
            ctx.resources.get(AIClient),
            user_id=ctx.envelope.user_id,
            source_item_id=payload.source_item_id,
        )


def _now(ctx: HandlerContext) -> datetime.datetime:
    try:
        return ctx.resources.get(Clock).now()
    except LookupError:
        return datetime.datetime.now(datetime.UTC)


@handles(EXTRACTION_COMPLETED, name=APPLY_HANDLER, queue="apply")
async def on_extraction_completed(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, ExtractionCompleted)
    if payload.pipeline == "email_extract":
        await apply_extraction(ctx.tx, payload.extraction_id, now=_now(ctx))
    elif payload.pipeline == "meeting_extract":
        await apply_meeting_extraction(ctx.tx, payload.extraction_id, now=_now(ctx))


@handles(SOURCE_ITEM_STAGE_DUE, name=APPLY_DUE_HANDLER, queue="apply")
async def on_apply_due(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemStageDue)
    if payload.stage != "extracted":
        return
    pending = await latest_pending_for_source(ctx.tx, payload.source_item_id)
    if pending is not None:
        await apply_extraction(ctx.tx, pending.id, now=_now(ctx))


@handles(ADJUDICATION_NEEDED, name=ADJUDICATE_HANDLER, queue="ai_standard", mode="natural_key")
async def on_adjudication_needed(ctx: HandlerContext) -> None:
    """AI-02 (§8.3). Disabled until experiment X1: the role is disabled in ``config/models.yaml``,
    so the call returns ``role_disabled`` without reaching the provider and the provisional item
    stays ``suggested`` with band ``low`` ("please confirm", AI_PIPELINE.md §14)."""
    payload = ctx.payload
    assert isinstance(payload, AdjudicationNeeded) and ctx.envelope.user_id is not None
    client = ctx.resources.get(AIClient)
    try:
        client.registry.role("adjudicate")
    except Exception:
        log.info("adjudication_disabled", work_item_id=str(payload.work_item_id))
        return
    async with ctx.factory(user_id=ctx.envelope.user_id) as uow:
        ext = await get_extraction(uow, payload.extraction_id)
        inp = await _build_input(uow, ext.source_item_id)
    statements = (ext.output or {}).get("statements", [])
    quote = (
        statements[payload.statement_index]["evidence_quote"]
        if payload.statement_index < len(statements)
        else ""
    )
    outcome = await run_adjudication(client, inp, quote, user_id=ctx.envelope.user_id)
    log.info("adjudication_result", ok=outcome.ok, error=outcome.error_code)


@handles(SOURCE_ITEM_DELETED, name="work.source_deleted", queue="apply")
async def on_source_item_deleted(ctx: HandlerContext) -> None:
    """§9.3: redact evidence, archive or flag items; no AI call."""
    payload = ctx.payload
    assert isinstance(payload, SourceItemDeleted)
    await on_source_deleted(ctx.tx, payload.source_item_id, now=_now(ctx))
