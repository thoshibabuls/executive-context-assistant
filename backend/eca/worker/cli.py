"""Operator CLI (BACKEND_DESIGN.md §14.4): ``eca ops outbox retry`` and ``eca ops reembed``.

Runs as the worker role (``API_WORKER_DATABASE_URL``).
- ``outbox retry``: ``failed`` outbox rows → ``pending``.
- ``reembed`` (Phase 2): re-embed one user's chunks whose embedding model differs from the
  current AI-04 model, after an embedding model change (AI_PIPELINE.md §10). Never scheduled;
  ``--dry-run`` only counts.
- ``vapid-keys`` (Phase 3): print a new Web Push VAPID key pair for the operator to store as
  ``WEB_PUSH_VAPID_PUBLIC_KEY`` / ``WEB_PUSH_VAPID_PRIVATE_KEY`` in the secret manager
  (TECHNICAL_DESIGN.md §15.4). Nothing is written to disk; needs no database.
- ``cost --date YYYY-MM-DD`` (Phase 3): AI cost per user (hashed) and role for one UTC day, from
  ``ai_cost_rollups`` (AI_COST_MODEL.md §8).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from eca.attention import generate_keys
from eca.intelligence import build_ai_client, cost_by_user
from eca.platform.config import Settings, get_settings
from eca.platform.db import create_engine, create_session_factory
from eca.platform.outbox import retry_failed
from eca.platform.runtime import ensure_selector_event_loop_policy
from eca.platform.uow import UnitOfWorkFactory
from eca.retrieval import reembed_stale


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eca")
    sub = parser.add_subparsers(dest="group", required=True)
    ops = sub.add_parser("ops", help="operator commands").add_subparsers(dest="area", required=True)
    outbox = ops.add_parser("outbox").add_subparsers(dest="action", required=True)
    retry = outbox.add_parser("retry", help="re-queue failed outbox rows (failed -> pending, attempts = 0)")
    retry.add_argument("--event-id", type=UUID, default=None)
    reembed = ops.add_parser("reembed", help="re-embed chunks of an older embedding model (one user)")
    reembed.add_argument("--user", type=UUID, required=True)
    reembed.add_argument("--limit", type=int, default=500)
    reembed.add_argument("--dry-run", action="store_true")
    ops.add_parser("vapid-keys", help="print a new Web Push VAPID key pair (store it as secrets)")
    cost = ops.add_parser("cost", help="AI cost per user (hashed) and role for one UTC day")
    cost.add_argument("--date", type=datetime.date.fromisoformat, required=True)
    return parser


def _factory(settings: Settings) -> tuple[UnitOfWorkFactory, AsyncEngine]:
    url = settings.api_worker_database_url
    if url is None or not url.get_secret_value():
        raise SystemExit("API_WORKER_DATABASE_URL is required")
    engine = create_engine(url.get_secret_value(), pool_size=1, max_overflow=0)
    return UnitOfWorkFactory(create_session_factory(engine)), engine


async def outbox_retry(settings: Settings, event_id: UUID | None) -> int:
    factory, engine = _factory(settings)
    try:
        async with factory(user_id=None) as uow:
            return await retry_failed(uow, event_id=event_id)
    finally:
        await engine.dispose()


async def reembed(settings: Settings, user_id: UUID, limit: int, dry_run: bool) -> str:
    factory, engine = _factory(settings)
    try:
        client = build_ai_client(settings, uow_factory=factory)
        report = await reembed_stale(
            factory,
            client,
            user_id=user_id,
            limit=limit,
            dry_run=dry_run,
            now=datetime.datetime.now(datetime.UTC),
        )
    finally:
        await engine.dispose()
    return f"{report.stale} chunk(s) with another embedding model; re-embedded {report.embedded}"


async def cost_report(settings: Settings, day: datetime.date) -> str:
    factory, engine = _factory(settings)
    try:
        async with factory(user_id=None) as uow:
            rows = await cost_by_user(uow, day=day)
    finally:
        await engine.dispose()
    lines = [" ".join((r.user_ref, r.role, str(r.calls), f"{r.est_cost_usd:.6f}")) for r in rows]
    return "\n".join(["user role calls est_cost_usd", *lines])


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ensure_selector_event_loop_policy()
    if args.area == "vapid-keys":
        public, private = generate_keys()
        print(f"WEB_PUSH_VAPID_PUBLIC_KEY={public}")
        print(f"WEB_PUSH_VAPID_PRIVATE_KEY={private}")
        print("# Store both in the secret manager; never commit them. Set WEB_PUSH_VAPID_SUBJECT too.")
        return 0
    if args.area == "cost":
        print(asyncio.run(cost_report(get_settings(), args.date)))
        return 0
    if args.area == "reembed":
        print(asyncio.run(reembed(get_settings(), args.user, args.limit, args.dry_run)))
        return 0
    count = asyncio.run(outbox_retry(get_settings(), args.event_id))
    print(f"re-queued {count} failed outbox event(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
