"""RT-05: an outbox row dispatched twice (BACKEND_DESIGN.md §7.4, §21; IMPLEMENTATION_PLAN.md slice 0.3).

The dispatcher crashes at ``dispatch.after_defer`` or ``dispatch.before_commit``: the job exists
but the row is still ``pending``, so it is dispatched again. The duplicate is

* rejected by ``queueing_lock`` while the first job is still queued,
* serialized by ``lock`` while the first job runs, and a no-op through ``event_consumptions``,
* a no-op through ``event_consumptions`` after the first job committed.

Each handler's effect occurs once. Real subprocesses, real crashes, real PostgreSQL.
"""

from __future__ import annotations

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
from tests.reliability.support.synthetic import FANOUT, SLOW, SYNTHETIC

pytestmark = pytest.mark.db

USER_A = uuid.UUID("00000000-0000-7000-8000-00000000a005")
HANDLER_JOBS = " WHERE task_name LIKE 'eca.handler.%%'"


def _crash_after_jobs_exist(db: TempDatabase, tmp_path: Path, crash_point: str) -> None:
    crashed = start_worker(db, "dispatcher", tmp_path, crash=crash_point)
    assert crashed.wait_exit() == EXIT_CODE
    assert rows(db, "SELECT status, attempts FROM outbox") == [("pending", 0)]  # the mark never committed


def _redispatch(db: TempDatabase, tmp_path: Path, event_id: uuid.UUID):  # type: ignore[no-untyped-def]
    with running_worker(db, "dispatcher", tmp_path) as dispatcher:
        wait_until(lambda: outbox_status(db, event_id) == "dispatched", timeout=60)
    return dispatcher


@pytest.mark.parametrize("crash_point", ["dispatch.after_defer", "dispatch.before_commit"])
async def test_duplicate_while_first_job_is_queued_is_rejected_by_queueing_lock(
    rt_db: TempDatabase, tmp_path: Path, crash_point: str
) -> None:
    [event_id] = await publish_as_api(rt_db, FANOUT, user_id=USER_A)
    _crash_after_jobs_exist(rt_db, tmp_path, crash_point)
    assert (
        scalar(rt_db, "SELECT count(*) FROM procrastinate_jobs" + HANDLER_JOBS + " AND status = 'todo'") == 2
    )
    dispatcher = _redispatch(rt_db, tmp_path, event_id)
    assert len(dispatcher.events("job_already_enqueued")) == 2  # one per handler
    assert dispatcher.events("job_deferred") == []
    assert scalar(rt_db, "SELECT count(*) FROM procrastinate_jobs" + HANDLER_JOBS) == 2
    with running_worker(rt_db, "jobs", tmp_path):
        wait_until(lambda: effects(rt_db) >= 2 and jobs_settled(rt_db), timeout=60)
    assert effects(rt_db, "test_fanout_a") == 1
    assert effects(rt_db, "test_fanout_b") == 1
    assert scalar(rt_db, "SELECT count(*) FROM event_consumptions") == 2


@pytest.mark.parametrize("crash_point", ["dispatch.after_defer", "dispatch.before_commit"])
async def test_duplicate_after_first_job_committed_is_a_consumption_noop(
    rt_db: TempDatabase, tmp_path: Path, crash_point: str
) -> None:
    [event_id] = await publish_as_api(rt_db, SYNTHETIC, user_id=USER_A)
    _crash_after_jobs_exist(rt_db, tmp_path, crash_point)
    with running_worker(rt_db, "jobs", tmp_path):  # first job runs and commits; the row stays pending
        wait_until(lambda: effects(rt_db) == 1 and jobs_settled(rt_db), timeout=60)
    assert outbox_status(rt_db, event_id) == "pending"
    dispatcher = _redispatch(rt_db, tmp_path, event_id)
    assert len(dispatcher.events("job_deferred")) == 1  # first job is done, so a second job is accepted
    with running_worker(rt_db, "jobs", tmp_path) as jobs:
        wait_until(lambda: jobs_settled(rt_db), timeout=60)
    assert len(jobs.events("handler_duplicate_skipped")) == 1
    assert rows(rt_db, "SELECT status FROM procrastinate_jobs" + HANDLER_JOBS) == [
        ("succeeded",),
        ("succeeded",),
    ]
    assert effects(rt_db) == 1
    assert scalar(rt_db, "SELECT count(*) FROM event_consumptions") == 1


async def test_duplicate_while_first_job_runs_is_serialized_by_lock_then_noop(
    rt_db: TempDatabase, tmp_path: Path
) -> None:
    [event_id] = await publish_as_api(rt_db, SLOW, user_id=USER_A)
    _crash_after_jobs_exist(rt_db, tmp_path, "dispatch.after_defer")
    with running_worker(rt_db, "jobs", tmp_path) as jobs:
        wait_until(
            lambda: scalar(rt_db, "SELECT status FROM procrastinate_jobs" + HANDLER_JOBS) == "doing",
            timeout=60,
        )
        dispatcher = _redispatch(rt_db, tmp_path, event_id)  # while the slow first job is running
        assert len(dispatcher.events("job_deferred")) == 1  # queueing_lock only covers queued jobs
        wait_until(lambda: jobs_settled(rt_db), timeout=60)
    first, second = rows(
        rt_db,
        "SELECT j.id, max(e.at) FILTER (WHERE e.type = 'started'), "
        "max(e.at) FILTER (WHERE e.type = 'succeeded') "
        "FROM procrastinate_jobs j JOIN procrastinate_events e ON e.job_id = j.id "
        "WHERE j.task_name LIKE 'eca.handler.%%' GROUP BY j.id ORDER BY j.id",
    )
    assert second[1] >= first[2]  # the duplicate started only after the first finished (lock)
    assert len(jobs.events("handler_duplicate_skipped")) == 1
    assert effects(rt_db) == 1
    assert scalar(rt_db, "SELECT count(*) FROM event_consumptions") == 1
