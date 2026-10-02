"""``ai_calls`` meter and 15-minute cost roll-ups (AI_COST_MODEL.md §8, BACKEND_DESIGN.md §5.5).

Each provider call writes one content-free row in its own short transaction (same role, same
``app.user_id`` as the caller), so calls whose caller later rolls back are still counted. The
insert is one Core INSERT without RETURNING: the API role has INSERT and no SELECT on
``ai_calls`` (§7.6).

The roll-up runs as the worker role with ``app.user_id`` unset and recomputes the buckets of a
recent window in one transaction (delete, then insert grouped), so reruns give the same rows.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import structlog
from sqlalchemy import insert, text

from eca.intelligence.models import ai_calls_table
from eca.intelligence.provider.types import CallStatus, Usage
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWorkFactory

log = structlog.get_logger("eca.intelligence.meter")

BUCKET = datetime.timedelta(minutes=15)
DEFAULT_ROLLUP_WINDOW = datetime.timedelta(hours=1)


@dataclass(frozen=True)
class CallRecord:
    user_id: UUID | None
    role: str
    inventory_id: str
    model: str
    prompt_version: str | None
    schema_version: str | None
    usage: Usage
    latency_ms: int
    est_cost_usd: Decimal
    status: CallStatus
    attempt: int
    is_fallback: bool
    error_code: str | None = None


class Meter:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def record(self, rec: CallRecord) -> UUID | None:
        """Write one ``ai_calls`` row in its own transaction; returns its ID.

        A meter failure is logged and does not fail the AI call: the call has already been paid,
        and failing the caller would make it repeat the call.
        """
        call_id = uuid7()
        try:
            async with self._uow_factory(user_id=rec.user_id) as uow:
                await uow.session.execute(
                    insert(ai_calls_table).values(
                        id=call_id,
                        user_id=rec.user_id,
                        role=rec.role,
                        inventory_id=rec.inventory_id,
                        model=rec.model,
                        prompt_version=rec.prompt_version,
                        schema_version=rec.schema_version,
                        input_tokens=rec.usage.input_tokens,
                        cached_input_tokens=rec.usage.cached_input_tokens,
                        output_tokens=rec.usage.output_tokens,
                        thinking_tokens=rec.usage.thinking_tokens,
                        audio_seconds=Decimal(str(rec.usage.audio_seconds)),
                        latency_ms=rec.latency_ms,
                        est_cost_usd=rec.est_cost_usd,
                        status=rec.status.value,
                        error_code=rec.error_code,
                        attempt=rec.attempt,
                        is_fallback=rec.is_fallback,
                    )
                )
        except Exception as exc:
            log.error("ai_call_meter_failed", role=rec.role, error_type=type(exc).__name__)
            return None
        return call_id


_DELETE_WINDOW_SQL = text(
    "DELETE FROM ai_cost_rollups WHERE bucket_start >= :from_bucket AND bucket_start < :to_bucket"
)
_INSERT_WINDOW_SQL = text(
    """
    INSERT INTO ai_cost_rollups (
        bucket_start, user_id, role, model, calls, failed_calls, retry_calls, fallback_calls,
        input_tokens, cached_input_tokens, output_tokens, thinking_tokens, audio_seconds,
        latency_ms_total, est_cost_usd, computed_at)
    SELECT date_bin('15 minutes', created_at, TIMESTAMPTZ '2000-01-01 00:00:00+00'),
           user_id, role, model,
           count(*),
           count(*) FILTER (WHERE status <> 'ok'),
           count(*) FILTER (WHERE attempt > 1),
           count(*) FILTER (WHERE is_fallback),
           sum(input_tokens), sum(cached_input_tokens), sum(output_tokens), sum(thinking_tokens),
           sum(audio_seconds), sum(latency_ms), sum(est_cost_usd), now()
      FROM ai_calls
     WHERE created_at >= :from_bucket AND created_at < :to_bucket
     GROUP BY 1, 2, 3, 4
    """
)


def bucket_floor(moment: datetime.datetime) -> datetime.datetime:
    """Start of the 15-minute bucket containing ``moment`` (UTC-aligned)."""
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone-aware")
    utc = moment.astimezone(datetime.UTC)
    return utc.replace(minute=utc.minute - utc.minute % 15, second=0, microsecond=0)


async def rollup_costs(
    uow_factory: UnitOfWorkFactory,
    *,
    now: datetime.datetime,
    window: datetime.timedelta = DEFAULT_ROLLUP_WINDOW,
) -> int:
    """Recompute the buckets from ``now - window`` up to and including the current one.

    Returns the number of roll-up rows written. Idempotent for the same ``ai_calls`` content.
    """
    to_bucket = bucket_floor(now) + BUCKET
    from_bucket = bucket_floor(now - window)
    params = {"from_bucket": from_bucket, "to_bucket": to_bucket}
    async with uow_factory(user_id=None) as uow:
        await uow.session.execute(_DELETE_WINDOW_SQL, params)
        result = await uow.session.execute(_INSERT_WINDOW_SQL, params)
    written: int = result.rowcount  # type: ignore[attr-defined]
    log.info("ai_cost_rollup", buckets_from=from_bucket.isoformat(), rows=written)
    return written
