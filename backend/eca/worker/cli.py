"""Operator CLI (BACKEND_DESIGN.md §14.4). Slice 0.3 has one command: ``eca ops outbox retry``.

Runs as the worker role (``API_WORKER_DATABASE_URL``): ``failed`` outbox rows → ``pending``.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from uuid import UUID

from eca.platform.config import Settings, get_settings
from eca.platform.db import create_engine, create_session_factory
from eca.platform.outbox import retry_failed
from eca.platform.runtime import ensure_selector_event_loop_policy
from eca.platform.uow import UnitOfWorkFactory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eca")
    sub = parser.add_subparsers(dest="group", required=True)
    ops = sub.add_parser("ops", help="operator commands").add_subparsers(dest="area", required=True)
    outbox = ops.add_parser("outbox").add_subparsers(dest="action", required=True)
    retry = outbox.add_parser("retry", help="re-queue failed outbox rows (failed -> pending, attempts = 0)")
    retry.add_argument("--event-id", type=UUID, default=None)
    return parser


async def outbox_retry(settings: Settings, event_id: UUID | None) -> int:
    url = settings.api_worker_database_url
    if url is None or not url.get_secret_value():
        raise SystemExit("API_WORKER_DATABASE_URL is required")
    engine = create_engine(url.get_secret_value(), pool_size=1, max_overflow=0)
    try:
        async with UnitOfWorkFactory(create_session_factory(engine))(user_id=None) as uow:
            return await retry_failed(uow, event_id=event_id)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ensure_selector_event_loop_policy()
    count = asyncio.run(outbox_retry(get_settings(), args.event_id))
    print(f"re-queued {count} failed outbox event(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
