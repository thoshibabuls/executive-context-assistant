"""Synthetic event types and handlers for the slice 0.3 reliability tests.

Each handler appends one row to ``rt_effects`` inside the handler transaction. ``rt_effects`` has
no unique constraint on (event, handler), so a duplicate effect is visible instead of being
absorbed by a key. ``seen_app_user_id`` records the ``app.user_id`` the handler ran with.
"""

from __future__ import annotations

import asyncio

import psycopg
from pydantic import BaseModel
from sqlalchemy import text

from eca.platform.events import EventRegistry, HandlerContext, HandlerFunc
from eca.platform.rls import user_isolation_ddl

SYNTHETIC = "test.SyntheticHappened"  # one handler: test_effect
FANOUT = "test.FannedOut"  # two handlers: test_fanout_a, test_fanout_b
FLAKY = "test.Flaky"  # test_flaky raises on its first two runs
SLOW = "test.Slow"  # test_slow sleeps before its effect
ALWAYS_FAILS = "test.AlwaysFails"  # test_always_fails always raises
NO_HANDLERS = "test.NoHandlers"  # registered, no handler

SLOW_SECONDS = 1.5
FLAKY_FAILURES = 2


class SyntheticPayload(BaseModel):
    marker: str


class FlakyError(RuntimeError):
    pass


_INSERT_EFFECT = text(
    "INSERT INTO rt_effects (event_id, handler, user_id, seen_app_user_id, marker) "
    "VALUES (:event_id, :handler, :user_id, current_setting('app.user_id', true), :marker)"
)


def _effect(
    handler_name: str, *, sleep_s: float = 0.0, fail_runs: int = 0, always_fail: bool = False
) -> HandlerFunc:
    async def handler(ctx: HandlerContext) -> None:
        if always_fail or ctx.attempt < fail_runs:
            raise FlakyError(f"{handler_name} fails on run {ctx.attempt + 1}")
        if sleep_s:
            await asyncio.sleep(sleep_s)
        assert isinstance(ctx.payload, SyntheticPayload)
        await ctx.tx.session.execute(
            _INSERT_EFFECT,
            {
                "event_id": ctx.envelope.id,
                "handler": handler_name,
                "user_id": ctx.envelope.user_id,
                "marker": ctx.payload.marker,
            },
        )

    return handler


def build_registry() -> EventRegistry:
    """A fresh registry holding only the synthetic types and handlers."""
    registry = EventRegistry()
    for event_type in (SYNTHETIC, FANOUT, FLAKY, SLOW, ALWAYS_FAILS, NO_HANDLERS):
        registry.register_event(event_type, SyntheticPayload)
    registry.handles(SYNTHETIC, name="test_effect")(_effect("test_effect"))
    registry.handles(FANOUT, name="test_fanout_a")(_effect("test_fanout_a"))
    registry.handles(FANOUT, name="test_fanout_b")(_effect("test_fanout_b"))
    registry.handles(FLAKY, name="test_flaky")(_effect("test_flaky", fail_runs=FLAKY_FAILURES))
    registry.handles(SLOW, name="test_slow")(_effect("test_slow", sleep_s=SLOW_SECONDS))
    registry.handles(ALWAYS_FAILS, name="test_always_fails")(_effect("test_always_fails", always_fail=True))
    return registry


def create_rt_tables(admin_url: str, *, api_role: str, worker_role: str) -> None:
    """Create the test-only tables with the migration role (never through Alembic).

    ``rt_effects``: worker role SELECT and INSERT only; API role nothing.
    ``rt_business``: a user-owned business table under per-user RLS, written by the API role.
    """
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            """
            CREATE TABLE rt_effects (
              id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              event_id         uuid NOT NULL,
              handler          text NOT NULL,
              user_id          uuid NULL,
              seen_app_user_id text NULL,
              marker           text NULL,
              created_at       timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(f"REVOKE ALL ON rt_effects FROM {api_role}")
        conn.execute(f"REVOKE UPDATE, DELETE ON rt_effects FROM {worker_role}")
        conn.execute(
            "CREATE TABLE rt_business (id uuid PRIMARY KEY, user_id uuid NOT NULL, note text NOT NULL)"
        )
        for stmt in user_isolation_ddl("rt_business"):
            conn.execute(stmt)
