"""RT-01, infrastructure level (slice 0.3; BACKEND_DESIGN.md §21, IMPLEMENTATION_PLAN.md slice 0.3).

Proves the delivery guarantee of the event infrastructure with synthetic events and handlers:
transactional outbox writes, dispatch after crashes, handler registration, duplicate-delivery
safety, consumption dedupe, crash and retry recovery, effective exactly-once database effects.

It does NOT prove Gmail normalization, email or AI extraction, apply, the real message
lifecycle or ``source_items`` processing. RT-01 is extended to pipeline level in slices 1.3-1.4.

Every worker runs in a subprocess (``tests.reliability.support.worker_main``) and crashes are
real ``os._exit(97)`` at named crash points. ``rt_effects`` has no unique key, so a duplicate
effect would be counted.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from eca.platform.crashpoints import EXIT_CODE
from tests.conftest import TempDatabase
from tests.reliability.support.harness import (
    effects,
    jobs_settled,
    outbox_status,
    publish_as_api,
    rows,
    running_worker,
    scalar,
    start_worker,
    wait_until,
)
from tests.reliability.support.synthetic import FANOUT, FLAKY, FLAKY_FAILURES, SYNTHETIC

pytestmark = pytest.mark.db

USER_A = uuid.UUID("00000000-0000-7000-8000-00000000a001")
USER_B = uuid.UUID("00000000-0000-7000-8000-00000000b001")
STALLED_WAIT_S = 2.5  # > the launcher's 2 s stalled timeout
HANDLER_JOBS = " WHERE task_name LIKE 'eca.handler.%%'"  # periodic infrastructure jobs excluded


def _delivered_once(db: TempDatabase, event_id: uuid.UUID, handler: str) -> None:
    assert (
        scalar(
            db, "SELECT count(*) FROM rt_effects WHERE event_id = %s AND handler = %s", (event_id, handler)
        )
        == 1
    )
    assert (
        scalar(
            db,
            "SELECT count(*) FROM event_consumptions WHERE event_id = %s AND handler = %s",
            (event_id, handler),
        )
        == 1
    )


def _wait_delivered(db: TempDatabase, expected_effects: int) -> None:
    wait_until(lambda: effects(db) >= expected_effects and jobs_settled(db), timeout=60)


async def test_a_event_committed_while_no_dispatcher_runs_is_delivered_once(
    rt_db: TempDatabase, tmp_path: Path
) -> None:
    [event_id] = await publish_as_api(rt_db, SYNTHETIC, user_id=USER_A, business_note="a")
    assert outbox_status(rt_db, event_id) == "pending"
    assert scalar(rt_db, "SELECT count(*) FROM rt_business") == 1  # business row and event committed together
    with running_worker(rt_db, "all", tmp_path) as worker:
        _wait_delivered(rt_db, 1)
    assert worker.proc.returncode == 0  # graceful stop on SIGTERM
    _delivered_once(rt_db, event_id, "test_effect")
    assert outbox_status(rt_db, event_id) == "dispatched"
    assert rows(rt_db, "SELECT user_id, seen_app_user_id FROM rt_effects") == [(USER_A, str(USER_A))]


async def test_b_crash_after_claim_then_dispatch_on_restart(rt_db: TempDatabase, tmp_path: Path) -> None:
    [event_id] = await publish_as_api(rt_db, SYNTHETIC, user_id=USER_A)
    crashed = start_worker(rt_db, "dispatcher", tmp_path, crash="dispatch.after_claim")
    assert crashed.wait_exit() == EXIT_CODE
    assert rows(rt_db, "SELECT status, attempts FROM outbox") == [
        ("pending", 0)
    ]  # row lock released, no state
    assert scalar(rt_db, "SELECT count(*) FROM procrastinate_jobs" + HANDLER_JOBS) == 0
    with running_worker(rt_db, "all", tmp_path):
        _wait_delivered(rt_db, 1)
    _delivered_once(rt_db, event_id, "test_effect")
    assert effects(rt_db) == 1


async def test_c_rolled_back_transaction_leaves_no_event_and_no_effect(
    rt_db: TempDatabase, tmp_path: Path
) -> None:
    rolled_back = await publish_as_api(rt_db, SYNTHETIC, user_id=USER_A, business_note="c", rollback=True)
    assert scalar(rt_db, "SELECT count(*) FROM outbox") == 0
    assert scalar(rt_db, "SELECT count(*) FROM rt_business") == 0
    [sentinel] = await publish_as_api(rt_db, SYNTHETIC, user_id=USER_A)  # proves the worker ran
    with running_worker(rt_db, "all", tmp_path):
        _wait_delivered(rt_db, 1)
    assert rows(rt_db, "SELECT event_id FROM rt_effects") == [(sentinel,)]
    assert sentinel not in rolled_back


@pytest.mark.parametrize("crash_point", ["handler.after_consumption", "handler.before_commit"])
async def test_d_crash_inside_handler_rolls_back_then_one_effect(
    rt_db: TempDatabase, tmp_path: Path, crash_point: str
) -> None:
    [event_id] = await publish_as_api(rt_db, SYNTHETIC, user_id=USER_A)
    crashed = start_worker(rt_db, "all", tmp_path, crash=crash_point)
    assert crashed.wait_exit(timeout=60) == EXIT_CODE
    # The handler transaction (consumption + effect) rolled back with the process.
    assert effects(rt_db) == 0
    assert scalar(rt_db, "SELECT count(*) FROM event_consumptions") == 0
    assert rows(rt_db, "SELECT status, attempts FROM procrastinate_jobs" + HANDLER_JOBS) == [("doing", 0)]
    await asyncio.sleep(STALLED_WAIT_S)
    with running_worker(rt_db, "all", tmp_path) as worker:  # start-up stalled-job recovery
        _wait_delivered(rt_db, 1)
    _delivered_once(rt_db, event_id, "test_effect")
    assert rows(rt_db, "SELECT status, attempts FROM procrastinate_jobs" + HANDLER_JOBS) == [("succeeded", 2)]
    recovered = [e for e in worker.events("stalled_job_recovered") if e["task"].startswith("eca.handler.")]
    assert len(recovered) == 1  # infrastructure jobs that were running at the crash are recovered too


async def test_e_crash_after_handler_commit_reruns_as_noop(rt_db: TempDatabase, tmp_path: Path) -> None:
    [event_id] = await publish_as_api(rt_db, SYNTHETIC, user_id=USER_A)
    crashed = start_worker(rt_db, "all", tmp_path, crash="handler.after_commit")
    assert crashed.wait_exit(timeout=60) == EXIT_CODE
    _delivered_once(rt_db, event_id, "test_effect")  # committed before the crash
    assert scalar(rt_db, "SELECT status FROM procrastinate_jobs" + HANDLER_JOBS) == "doing"
    await asyncio.sleep(STALLED_WAIT_S)
    with running_worker(rt_db, "all", tmp_path) as worker:
        wait_until(lambda: jobs_settled(rt_db), timeout=60)
    assert len(worker.events("handler_duplicate_skipped")) == 1  # the re-run hit the consumption conflict
    _delivered_once(rt_db, event_id, "test_effect")
    assert scalar(rt_db, "SELECT status FROM procrastinate_jobs" + HANDLER_JOBS) == "succeeded"


async def test_f_handler_failing_twice_then_succeeding_has_one_effect(
    rt_db: TempDatabase, tmp_path: Path
) -> None:
    [event_id] = await publish_as_api(rt_db, FLAKY, user_id=USER_A)
    with running_worker(rt_db, "all", tmp_path):
        _wait_delivered(rt_db, 1)
    _delivered_once(rt_db, event_id, "test_flaky")
    assert rows(rt_db, "SELECT status, attempts FROM procrastinate_jobs" + HANDLER_JOBS) == [
        ("succeeded", FLAKY_FAILURES + 1)
    ]


async def test_g_two_handlers_on_one_event_have_one_effect_each(rt_db: TempDatabase, tmp_path: Path) -> None:
    [event_id] = await publish_as_api(rt_db, FANOUT, user_id=USER_A)
    with running_worker(rt_db, "all", tmp_path):
        _wait_delivered(rt_db, 2)
    _delivered_once(rt_db, event_id, "test_fanout_a")
    _delivered_once(rt_db, event_id, "test_fanout_b")
    assert effects(rt_db) == 2


async def test_h_two_dispatchers_defer_each_event_once_per_handler(
    rt_db: TempDatabase, tmp_path: Path
) -> None:
    ids = await publish_as_api(rt_db, FANOUT, user_id=USER_A, count=30)
    ids += await publish_as_api(rt_db, FANOUT, user_id=USER_B, count=30)
    first = start_worker(rt_db, "dispatcher", tmp_path, batch_size=5)
    second = start_worker(rt_db, "dispatcher", tmp_path, batch_size=5)
    try:
        wait_until(
            lambda: scalar(rt_db, "SELECT count(*) FROM outbox WHERE status = 'dispatched'") == 60, timeout=60
        )
    finally:
        assert first.stop() == 0 and second.stop() == 0
    deferred = first.events("job_deferred") + second.events("job_deferred")
    assert len(deferred) == 120
    assert len({(e["event_id"], e["handler"]) for e in deferred}) == 120
    assert first.events("job_already_enqueued") + second.events("job_already_enqueued") == []
    assert scalar(rt_db, "SELECT count(*) FROM procrastinate_jobs" + HANDLER_JOBS) == 120
    with running_worker(rt_db, "jobs", tmp_path):
        _wait_delivered(rt_db, 120)
    assert effects(rt_db) == 120
    assert scalar(rt_db, "SELECT count(DISTINCT (event_id, handler)) FROM rt_effects") == 120
    assert {r[0] for r in rows(rt_db, "SELECT DISTINCT user_id FROM rt_effects")} == {USER_A, USER_B}
