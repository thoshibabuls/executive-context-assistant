"""Deletion and retention in ``intelligence`` (BACKEND_DESIGN.md §13.3; AI_COST_MODEL.md).

- ``extraction_ids_for_sources`` / ``delete_extractions``: source purge. Callers clear references
  (evidence, work items, events, triage) first; parent links are cleared before deletion.
- ``purge_user``: account deletion. Extractions are deleted; ``ai_calls`` keep no user link
  (``user_id`` nulled: cost history stays, identity goes); the user's ``ai_cost_rollups`` are
  folded into the system (NULL-user) rows of the same bucket, role and model, then deleted.
- ``purge_old_calls``: ``ai_calls`` older than 90 days (rollups keep the aggregates).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, select, text, update

from eca.intelligence.models import ai_calls_table, extractions_table
from eca.platform.uow import UnitOfWork

AI_CALLS_RETENTION_DAYS = 90

# The user's rollups folded into the system (NULL-user) row of the same bucket, role and model.
_FOLD_ROLLUPS = text(
    """
    INSERT INTO ai_cost_rollups (bucket_start, user_id, role, model, calls, failed_calls,
           retry_calls, fallback_calls, input_tokens, cached_input_tokens, output_tokens,
           thinking_tokens, audio_seconds, latency_ms_total, est_cost_usd, computed_at)
    SELECT bucket_start, NULL, role, model, calls, failed_calls, retry_calls, fallback_calls,
           input_tokens, cached_input_tokens, output_tokens, thinking_tokens, audio_seconds,
           latency_ms_total, est_cost_usd, now()
      FROM ai_cost_rollups WHERE user_id = :user_id
    ON CONFLICT (bucket_start, user_id, role, model) DO UPDATE
       SET calls = ai_cost_rollups.calls + EXCLUDED.calls,
           failed_calls = ai_cost_rollups.failed_calls + EXCLUDED.failed_calls,
           retry_calls = ai_cost_rollups.retry_calls + EXCLUDED.retry_calls,
           fallback_calls = ai_cost_rollups.fallback_calls + EXCLUDED.fallback_calls,
           input_tokens = ai_cost_rollups.input_tokens + EXCLUDED.input_tokens,
           cached_input_tokens = ai_cost_rollups.cached_input_tokens + EXCLUDED.cached_input_tokens,
           output_tokens = ai_cost_rollups.output_tokens + EXCLUDED.output_tokens,
           thinking_tokens = ai_cost_rollups.thinking_tokens + EXCLUDED.thinking_tokens,
           audio_seconds = ai_cost_rollups.audio_seconds + EXCLUDED.audio_seconds,
           latency_ms_total = ai_cost_rollups.latency_ms_total + EXCLUDED.latency_ms_total,
           est_cost_usd = ai_cost_rollups.est_cost_usd + EXCLUDED.est_cost_usd,
           computed_at = now()
    """
)


async def extraction_ids_for_sources(uow: UnitOfWork, source_ids: Sequence[UUID]) -> list[UUID]:
    x = extractions_table
    rows = await uow.session.execute(select(x.c.id).where(x.c.source_item_id.in_(list(source_ids))))
    return sorted(r.id for r in rows)


async def delete_extractions(uow: UnitOfWork, extraction_ids: Sequence[UUID]) -> None:
    ids = list(extraction_ids)
    if not ids:
        return
    x = extractions_table
    await uow.session.execute(
        update(x).where(x.c.parent_extraction_id.in_(ids)).values(parent_extraction_id=None)
    )
    await uow.session.execute(delete(x).where(x.c.id.in_(ids)))


async def purge_user(uow: UnitOfWork) -> None:
    assert uow.user_id is not None
    x, calls = extractions_table, ai_calls_table
    await uow.session.execute(update(x).values(parent_extraction_id=None))
    await uow.session.execute(delete(x))
    await uow.session.execute(update(calls).where(calls.c.user_id == uow.user_id).values(user_id=None))
    await uow.session.execute(_FOLD_ROLLUPS, {"user_id": uow.user_id})
    await uow.session.execute(
        text("DELETE FROM ai_cost_rollups WHERE user_id = :user_id"), {"user_id": uow.user_id}
    )


async def purge_old_calls(uow: UnitOfWork, *, now: datetime.datetime) -> int:
    calls = ai_calls_table
    cutoff = now - datetime.timedelta(days=AI_CALLS_RETENTION_DAYS)
    result = await uow.session.execute(delete(calls).where(calls.c.created_at < cutoff))
    return int(result.rowcount)  # type: ignore[attr-defined]
