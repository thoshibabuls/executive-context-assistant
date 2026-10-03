"""AI-03 thread summaries (slice 3.2; AI_PIPELINE.md §5.9; BACKEND_DESIGN.md §5.2, §15).

``retrieval`` orchestrates (it already imports ``communication`` and ``intelligence`` and runs
AI-04 the same way); ``communication`` stays the single writer of ``conversations.summary_*``.

- ``thread_summary_sweep`` (every 5 minutes): per user, claims threads with ≥ 8 relevant messages
  whose newest relevant message is at least 10 minutes old and publishes ``ThreadSummaryDue``.
- ``request_summary``: the user's "Summarize" (any thread with ≥ 2 relevant messages); 429 through
  ``BudgetExceeded`` at the hard cap.
- ``summarize``: the natural-key handler body: transaction 1 reads clean messages under the
  advisory lock ``summary:{conversation}``; the model call runs outside any transaction;
  transaction 2 stores the result only while the claim still holds. Failure → gist timeline.
"""

from __future__ import annotations

import datetime
from uuid import UUID

import structlog
from sqlalchemy import text

from eca import communication, identity, intelligence, people
from eca.intelligence import AIClient, BudgetLevel
from eca.platform.errors import BudgetExceeded
from eca.platform.events import NewEvent
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory
from eca.retrieval.cards import delimit, fmt_date
from eca.retrieval.chunking import estimate_tokens
from eca.retrieval.events import THREAD_SUMMARY_DUE, ThreadSummaryDue
from eca.retrieval.temporal import zone

log = structlog.get_logger("eca.retrieval.summaries")

MESSAGE_TOKENS = 400
INPUT_TOKENS = 6000
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended('summary:' || :conversation, 0))")


async def _lock(uow: UnitOfWork, conversation_id: UUID) -> None:
    await uow.session.execute(_LOCK_SQL, {"conversation": str(conversation_id)})


async def _publish(uow: UnitOfWork, conversation_id: UUID, through: UUID, requested: bool) -> None:
    await publish(
        uow,
        NewEvent(
            event_type=THREAD_SUMMARY_DUE,
            aggregate_type="conversation",
            aggregate_id=conversation_id,
            payload=ThreadSummaryDue(
                conversation_id=conversation_id, through_message_id=through, requested=requested
            ),
        ),
    )


async def request_summary(uow: UnitOfWork, conversation_id: UUID, *, now: datetime.datetime) -> bool:
    """The user's request. True when a job was queued; False when the stored summary already
    covers the newest relevant message, a job is already queued, or the thread is too short."""
    level = await intelligence.budget_level(uow, now=now)
    if level is BudgetLevel.HARD:  # AI-03 stops at the hard cap (AI_COST_MODEL.md §7.2)
        raise BudgetExceeded(
            "The daily AI budget is used up; summaries resume tomorrow.",
            retry_after_s=intelligence.seconds_until_reset(now),
            details={"role": "thread_summary", "level": level.value},
        )
    await communication.thread_summary(uow, conversation_id)  # 404 for unknown or other users' threads
    await _lock(uow, conversation_id)
    through = await communication.claim_summary(uow, conversation_id, requested=True, now=now)
    if through is None:
        return False
    await _publish(uow, conversation_id, through, requested=True)
    return True


async def sweep_user(uow: UnitOfWork, *, now: datetime.datetime) -> int:
    queued = 0
    for conversation_id in await communication.summary_candidates(uow, now=now):
        through = await communication.claim_summary(uow, conversation_id, requested=False, now=now)
        if through is not None:
            await _publish(uow, conversation_id, through, requested=False)
            queued += 1
    return queued


async def sweep_all(factory: UnitOfWorkFactory, *, now: datetime.datetime) -> int:
    async with factory(user_id=None) as uow:
        users = await identity.list_active_user_ids(uow)
    total = 0
    for user_id in users:
        async with factory(user_id=user_id) as uow:
            total += await sweep_user(uow, now=now)
    log.info("thread_summary_sweep", users=len(users), queued=total)
    return total


def _cut(value: str, tokens: int) -> str:
    limit = tokens * 4
    return value if len(value) <= limit else value[: limit - 1] + "…"


def render_messages(
    inp: communication.SummaryInput, names: dict[UUID, str], self_id: UUID, tz_name: str
) -> str:
    """``[Mn] date · sender · direction`` and the delimited clean text; older messages are dropped
    first when the input exceeds 6,000 tokens."""
    tz = zone(tz_name)
    blocks: list[str] = []
    total = estimate_tokens(inp.subject or "")
    for msg in reversed(inp.messages):
        sender = (
            "the user"
            if msg.sender_person_id == self_id
            else names.get(msg.sender_person_id or self_id, "unknown")
        )
        kind = " (gist; body no longer kept)" if msg.from_gist else ""
        head = f"[{msg.ref}] {fmt_date(msg.sent_at, tz, with_time=True)} · {delimit(sender)}"
        head += " (the user)" if msg.sender_person_id == self_id else ""
        block = f"{head} · {msg.direction}{kind}\n{delimit(_cut(msg.text, MESSAGE_TOKENS))}"
        cost = estimate_tokens(block)
        if blocks and total + cost > INPUT_TOKENS:
            break
        blocks.insert(0, block)
        total += cost
    return f"SUBJECT: {delimit(inp.subject or '')}\n\n" + "\n\n".join(blocks)


async def summarize(
    factory: UnitOfWorkFactory,
    client: AIClient,
    *,
    user_id: UUID,
    conversation_id: UUID,
    through_message_id: UUID,
    now: datetime.datetime,
) -> str:
    async with factory(user_id=user_id) as uow:
        await _lock(uow, conversation_id)
        inp = await communication.summary_input(uow, conversation_id, through_message_id)
        if inp is None or not inp.messages:
            return "skipped"  # summarized already, or the claim moved to a newer message
        settings = await identity.get_user_settings(uow)
        self_id = (await people.get_self_person(uow)).id
        persons = await people.get_persons(
            uow, [m.sender_person_id for m in inp.messages if m.sender_person_id]
        )
        names = {pid: (p.display_name or p.primary_email or "unknown") for pid, p in persons.items()}
        block = render_messages(inp, names, self_id, settings.timezone)
    call = await intelligence.run_thread_summary(client, block, user_id=user_id)
    refs = {m.ref: m for m in inp.messages}
    async with factory(user_id=user_id) as uow:
        await _lock(uow, conversation_id)
        if call.output is None:
            budget = call.degraded == "budget_exceeded"
            await communication.release_summary_claim(
                uow, conversation_id, through_message_id=through_message_id, failed=not budget
            )
            log.info("thread_summary_degraded", reason=call.degraded)
            return "degraded"
        key_points = []
        for point in call.output.key_points:
            cited = [refs[r] for r in point.messages if r in refs]
            if not cited:
                continue  # a key point citing no existing message is dropped (§5.9)
            key_points.append(
                {
                    "text": point.text,
                    "message_refs": [m.ref for m in cited],
                    "source_item_ids": [str(m.source_item_id) for m in cited],
                }
            )
        stored = await communication.store_thread_summary(
            uow,
            conversation_id,
            through_message_id=through_message_id,
            summary=call.output.summary,
            key_points=key_points,
            model=call.model,
            prompt_version=call.prompt_version,
            ai_call_ids=list(call.call_ids),
            covered_source_ids=[m.source_item_id for m in inp.messages],
            now=now,
        )
    return "stored" if stored else "superseded"
