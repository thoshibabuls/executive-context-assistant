"""RT-09: two reminder sweeps run concurrently (BACKEND_DESIGN.md §21, TECHNICAL_DESIGN.md §15).

"One reminder, one notification per channel." An overdue task of a user with a Web Push
subscription is swept by two sweeps at the same instant on separate connections, twice in a row.
Synthetic data only; no push request leaves the process (no VAPID keys, so ``ReminderDue`` is
only published).
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Iterator
from pathlib import Path

import pytest

from eca.attention.push import subscribe
from eca.attention.reminders import sweep_all
from eca.platform.db import create_engine, create_session_factory
from eca.platform.uow import UnitOfWorkFactory
from tests.api.support import ApiHarness, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

DUE = datetime.datetime(2026, 10, 5, 17, 0, tzinfo=datetime.UTC)  # Monday 10:00 in Los Angeles
NOW = datetime.datetime(2026, 10, 7, 18, 0, tzinfo=datetime.UTC)  # Wednesday 11:00, outside quiet hours


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


async def _concurrent_sweeps(h: ApiHarness, user_id: object, *, subscribe_first: bool) -> None:
    engines = [create_engine(h.db.worker_url, pool_size=2, max_overflow=0) for _ in range(2)]
    try:
        factories = [UnitOfWorkFactory(create_session_factory(e)) for e in engines]
        if subscribe_first:
            api = create_engine(h.db.runtime_url, pool_size=1, max_overflow=0)
            try:
                async with UnitOfWorkFactory(create_session_factory(api))(user_id=user_id) as uow:  # type: ignore[arg-type]
                    await subscribe(
                        uow,
                        endpoint="https://fcm.googleapis.com/fcm/send/test-endpoint",
                        expires_at=None,
                        user_agent=None,
                    )
            finally:
                await api.dispose()
        await asyncio.wait_for(asyncio.gather(*(sweep_all(f, now=NOW) for f in factories)), timeout=60)
    finally:
        for e in engines:
            await e.dispose()


def test_rt09_concurrent_sweeps_give_one_reminder_and_one_notification_per_channel(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    r = h.request(
        avery,
        "POST",
        "/api/v1/work-items",
        json={
            "title": "File the expense report",
            "due_at": DUE.isoformat(),
            "owner_person_id": str(avery.self_person_id),
        },
    )
    assert r.status_code == 201
    asyncio.run(_concurrent_sweeps(h, avery.user_id, subscribe_first=True))
    asyncio.run(_concurrent_sweeps(h, avery.user_id, subscribe_first=False))  # a later pair of sweeps

    assert h.rows("SELECT reminder_type, state FROM reminders") == [("overdue", "delivered")]
    assert sorted(h.rows("SELECT channel, seq FROM notifications")) == [("in_app", 1), ("web_push", 1)]
    assert h.scalar("SELECT count(*) FROM outbox WHERE event_type = 'ReminderDue'") == 1
    listed = h.request(avery, "GET", "/api/v1/reminders").json()["items"]
    assert len(listed) == 1 and "File the expense report" in listed[0]["text"]
