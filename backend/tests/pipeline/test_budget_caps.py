"""Slice 3.5a budget guardrails (AI_COST_MODEL.md §7.2, IMPLEMENTATION_PLAN.md §0.7 deferred tests).

Per-role soft and hard caps per user, the AI-01 VIP/outbound exemption, the per-role daily call
cap, the global budget (worker, background roles only), ``BudgetExceeded`` → 429 with
``Retry-After``, AI-01 deferral at the hard cap without an attempt or a model call, and the
budget-cap audit rows. Spend comes from synthetic ``ai_cost_rollups`` rows.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest
from pydantic import BaseModel

from eca.api.problems import status_for
from eca.connectors import NormalizedMessage, NormalizedPerson
from eca.ingestion import sync_mail
from eca.intelligence import load_ai_config
from eca.intelligence.budget import (
    GLOBAL_ALERT_ACTION,
    GLOBAL_REACHED_ACTION,
    HARD_CAP_ACTION,
    SOFT_CAP_ACTION,
    BudgetGuard,
    audit_budget_caps,
    default_budget_config,
    seconds_until_reset,
)
from eca.intelligence.provider.client import AIClient
from eca.platform.errors import BudgetExceeded
from tests.conftest import TempDatabase
from tests.fake_ai import FakeProvider
from tests.pipeline.support import Pipeline, Tenant, pipeline

pytestmark = pytest.mark.db

NOW = datetime.datetime.now(datetime.UTC)


_BUCKETS = iter(range(1, 10_000))


def _spend(
    p: Pipeline, user_id: UUID | None, usd: str, *, role: str = "answer_lookup", calls: int = 1
) -> None:
    """One synthetic roll-up row today (each in its own 15-minute bucket)."""
    day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    bucket = day + datetime.timedelta(seconds=next(_BUCKETS))
    with psycopg.connect(p.db.admin_url) as conn:
        conn.execute(
            "INSERT INTO ai_cost_rollups (bucket_start, user_id, role, model, calls, failed_calls, "
            "retry_calls, fallback_calls, input_tokens, cached_input_tokens, output_tokens, "
            "thinking_tokens, audio_seconds, latency_ms_total, est_cost_usd) "
            "VALUES (%s, %s, %s, 'm', %s, 0, 0, 0, 0, 0, 0, 0, 0, 0, %s)",
            (bucket, user_id, role, calls, Decimal(usd)),
        )


def _guard(p: Pipeline, *, global_scope: bool = False) -> BudgetGuard:
    return BudgetGuard(p.worker, default_budget_config(), global_scope=global_scope)


async def _allowed(guard: BudgetGuard, role: str, user_id: UUID | None, *, exempt: bool = False) -> bool:
    try:
        await guard.check(role, user_id=user_id, exempt=exempt)
    except BudgetExceeded as exc:
        assert exc.retry_after_s is not None and 0 < exc.retry_after_s <= 86_400
        return False
    return True


async def test_soft_and_hard_caps_per_role(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        t = await p.tenant()
        assert await _allowed(_guard(p), "answer_synthesis", t.user_id)
        _spend(p, t.user_id, "1.20")  # soft cap (1.00) reached
        g = _guard(p)
        assert not await _allowed(g, "answer_synthesis", t.user_id)  # stop_at soft
        assert not await _allowed(g, "meeting_asks", t.user_id)
        assert await _allowed(g, "answer_lookup", t.user_id)  # stop_at hard
        assert await _allowed(g, "email_extract", t.user_id)
        _spend(p, t.user_id, "1.50", role="email_extract", calls=2)  # 2.70: hard cap (2.50) reached
        g = _guard(p)
        for role in (
            "answer_lookup",
            "plan_query",
            "email_extract",
            "embed",
            "transcribe",
            "meeting_extract",
        ):
            assert not await _allowed(g, role, t.user_id), role
        assert await _allowed(g, "email_extract", t.user_id, exempt=True)  # VIP / outbound exemption
        assert not await _allowed(g, "thread_summary", t.user_id, exempt=True)  # not exempt_allowed
        assert await _allowed(g, "judge", t.user_id)  # stop_at never
        other = await p.tenant(email="blake@tallgrass.example", name="Blake Ortiz")
        assert await _allowed(g, "answer_synthesis", other.user_id)  # caps are per user


async def test_daily_call_cap_and_global_budget(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        t = await p.tenant()
        _spend(p, t.user_id, "0.01", role="adjudicate", calls=20)
        assert not await _allowed(_guard(p), "adjudicate", t.user_id)  # 20 AI-02 calls per day
        other = await p.tenant(email="blake@tallgrass.example", name="Blake Ortiz")
        _spend(p, None, "30.00", role="embed")  # system spend: the global budget (25.00) is gone
        api_guard, worker_guard = _guard(p), _guard(p, global_scope=True)
        assert await _allowed(api_guard, "embed", other.user_id)  # the API never enforces it
        assert not await _allowed(worker_guard, "embed", other.user_id)  # background role in the worker
        assert await _allowed(worker_guard, "answer_lookup", other.user_id)  # interactive role


async def test_budget_exceeded_is_429_with_retry_after() -> None:
    exc = BudgetExceeded("cap", retry_after_s=seconds_until_reset(NOW))
    assert status_for(exc) == (429, "budget_exceeded")
    assert 0 < seconds_until_reset(NOW) <= 86_400
    assert seconds_until_reset(datetime.datetime(2026, 10, 3, 23, 59, 59, tzinfo=datetime.UTC)) == 1


class _Echo(BaseModel):
    ok: bool = True


async def test_the_guard_runs_inside_the_ai_client_before_the_provider(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        t = await p.tenant()
        _spend(p, t.user_id, "3.00")
        provider = FakeProvider()
        cfg = load_ai_config()
        client = AIClient(
            registry=cfg.registry, prices=cfg.prices, mode="live", provider=provider, budget=_guard(p)
        )
        with pytest.raises(BudgetExceeded):
            await client.generate(
                "answer_lookup",
                prompt_version="v1",
                schema_version="s1",
                output_model=_Echo,
                contents=["x"],
                user_id=t.user_id,
            )
        with pytest.raises(BudgetExceeded):
            await client.embed("embed", ["x"], user_id=t.user_id)
        assert provider.generate_calls == [] and provider.embed_calls == []


async def test_ai01_is_deferred_at_the_hard_cap_without_an_attempt(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db, start=NOW) as p:  # the guard reads today's real spend
        t: Tenant = await p.tenant()
        _spend(p, t.user_id, "3.00")
        cfg = load_ai_config()
        p.ai = AIClient(
            registry=cfg.registry, prices=cfg.prices, mode="live", provider=p.fake_ai, budget=_guard(p)
        )
        p.feed(t).add(
            NormalizedMessage(
                external_id="B-1",
                thread_external_id="<b1@test.example>",
                rfc822_id="<b1@test.example>",
                in_reply_to=None,
                sent_at=datetime.datetime(2026, 9, 21, 9, 0, tzinfo=datetime.UTC),
                sender=NormalizedPerson("raj.menon@kestrelbank.example", "Raj Menon"),
                to=(NormalizedPerson(t.email, "Avery Lindqvist"),),
                cc=(),
                subject="Numbers",
                body_text="Could you send the Q3 numbers by Friday?",
                body_html=None,
                categories=("inbox",),
            )
        )
        await sync_mail(
            p.worker,
            p.connectors,
            user_id=t.user_id,
            connection_id=t.connection_id,
            now=p.clock.now(),
            owner="b",
        )
        await p.drain()
        row = p.rows("SELECT stage, last_error_code, next_attempt_at > now() FROM source_items")[0]
        assert row == ("extract_pending", "budget_deferred", True)
        assert p.fake_ai.calls_for("EmailExtraction") == []
        attempts: Any = p.rows("SELECT coalesce(max(attempts), 0) FROM extractions")
        assert attempts == [(0,)]


async def test_cap_audit_rows_are_written_once_per_day(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        soft = await p.tenant()
        hard = await p.tenant(email="blake@tallgrass.example", name="Blake Ortiz")
        _spend(p, soft.user_id, "1.10")
        _spend(p, hard.user_id, "2.60")
        _spend(p, None, "16.00", role="embed")  # 19.70 in total: above the 70% alert, below the budget
        written = await audit_budget_caps(p.worker, now=NOW)
        assert written == 4
        assert await audit_budget_caps(p.worker, now=NOW) == 0  # once per user, cap and day
        rows = sorted(p.rows("SELECT action, user_id IS NULL FROM audit_log"))
        assert rows == sorted(
            [
                (SOFT_CAP_ACTION, False),
                (SOFT_CAP_ACTION, False),
                (HARD_CAP_ACTION, False),
                (GLOBAL_ALERT_ACTION, True),
            ]
        )
        _spend(p, None, "6.00", role="embed")
        assert await audit_budget_caps(p.worker, now=NOW) == 1
        assert p.scalar("SELECT count(*) FROM audit_log WHERE action = %s", (GLOBAL_REACHED_ACTION,)) == 1


async def test_indexing_goes_fts_only_when_the_global_budget_is_spent(isolated_db: TempDatabase) -> None:
    """AI-04 refused by the budget: chunks are stored without vectors and the job is not retried."""
    async with pipeline(isolated_db, start=NOW) as p:
        t: Tenant = await p.tenant()
        _spend(p, None, "30.00", role="embed")  # the global budget (25.00) is spent
        cfg = load_ai_config()
        p.ai = AIClient(
            registry=cfg.registry,
            prices=cfg.prices,
            mode="live",
            provider=p.fake_ai,
            budget=_guard(p, global_scope=True),
        )
        p.feed(t).add(
            NormalizedMessage(
                external_id="B-2",
                thread_external_id="<b2@test.example>",
                rfc822_id="<b2@test.example>",
                in_reply_to=None,
                sent_at=datetime.datetime(2026, 9, 21, 9, 0, tzinfo=datetime.UTC),
                sender=NormalizedPerson("raj.menon@kestrelbank.example", "Raj Menon"),
                to=(NormalizedPerson(t.email, "Avery Lindqvist"),),
                cc=(),
                subject="Venue",
                body_text="The venue for the offsite is confirmed for the 14th.",
                body_html=None,
                categories=("inbox",),
            )
        )
        await sync_mail(
            p.worker,
            p.connectors,
            user_id=t.user_id,
            connection_id=t.connection_id,
            now=p.clock.now(),
            owner="g",
        )
        await p.drain()  # IndexRetry would raise here
        assert p.scalar("SELECT count(*) FROM chunks") >= 1
        assert p.scalar("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL") == 0
        assert p.fake_ai.embed_calls == []
