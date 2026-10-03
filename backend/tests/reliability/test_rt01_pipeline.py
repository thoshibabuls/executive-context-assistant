"""RT-01 at pipeline level, first half (slice 1.3): sync commit → SourceItemStored → normalized
exactly once, with the real worker process (production composition) and crash injection.

BACKEND_DESIGN.md §21: "After restart the event is dispatched; the message is normalized ...
exactly once." The extract and apply half is added in slice 1.4 (test_rt01_pipeline_full).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eca.ingestion import sync_mail
from tests.conftest import TempDatabase
from tests.pipeline.support import Pipeline, Tenant, pipeline, world_v1_messages
from tests.reliability.support.harness import running_worker, start_worker, wait_until

pytestmark = pytest.mark.db

LAUNCHER = "tests.reliability.support.pipeline_worker_main"
N = 20


def _replay_env(tmp_path: Path) -> dict[str, str]:
    """The production composition builds the AI client from settings; replay mode never reaches the
    network. The cassette directory is empty: these tests stop at normalization, and the extract
    and index handlers that follow fail on a cassette miss without touching the normalized rows."""
    cassettes = tmp_path / "cassettes"
    cassettes.mkdir(exist_ok=True)
    return {"API_AI_MODE": "replay", "API_AI_CASSETTE_DIR": str(cassettes)}


async def _synced(p: Pipeline) -> Tenant:
    t = await p.tenant()
    p.feed(t).extend(world_v1_messages()[:N])
    await sync_mail(
        p.worker,
        p.connectors,
        user_id=t.user_id,
        connection_id=t.connection_id,
        now=p.clock.now(),
        owner="rt01",
    )
    return t


def _settled(p: Pipeline) -> bool:
    return p.scalar("SELECT count(*) FROM source_items WHERE stage = 'fetched'") == 0


def _assert_normalized_exactly_once(p: Pipeline) -> None:
    assert p.scalar("SELECT count(*) FROM source_items") == N
    assert p.scalar("SELECT count(*) FROM messages") == N
    # One consumption of the normalize handler per SourceItemStored event; no event left pending.
    assert p.rows(
        "SELECT count(*), count(DISTINCT event_id) FROM event_consumptions "
        "WHERE handler = 'communication.normalize'"
    ) == [(N, N)]
    assert (
        p.scalar(
            "SELECT count(*) FROM outbox WHERE event_type = 'SourceItemStored' AND status <> 'dispatched'"
        )
        == 0
    )
    pending = p.scalar("SELECT count(*) FROM source_items WHERE stage = 'extract_pending'")
    assert p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'MessageNormalized'") == pending


@pytest.mark.parametrize("crash", ["dispatch.after_claim", "dispatch.after_defer", "dispatch.before_commit"])
async def test_rt01_pipeline_crash_after_sync_commit_before_dispatch(
    isolated_db: TempDatabase, tmp_path: Path, crash: str
) -> None:
    async with pipeline(isolated_db) as p:
        await _synced(p)
        assert p.scalar("SELECT count(*) FROM outbox WHERE status = 'pending'") == N
        crashed = start_worker(
            isolated_db,
            "dispatcher",
            tmp_path,
            crash=crash,
            launcher=LAUNCHER,
            extra_env=_replay_env(tmp_path),
        )
        assert crashed.wait_exit() == 97
        assert p.scalar("SELECT count(*) FROM messages") == 0
        with running_worker(isolated_db, "all", tmp_path, launcher=LAUNCHER, extra_env=_replay_env(tmp_path)):
            wait_until(lambda: _settled(p), timeout=60)
        _assert_normalized_exactly_once(p)


async def test_rt01_pipeline_crash_inside_the_normalize_handler(
    isolated_db: TempDatabase, tmp_path: Path
) -> None:
    async with pipeline(isolated_db) as p:
        await _synced(p)
        crashed = start_worker(
            isolated_db,
            "all",
            tmp_path,
            crash="handler.after_consumption",
            launcher=LAUNCHER,
            extra_env=_replay_env(tmp_path),
        )
        assert crashed.wait_exit() == 97
        assert p.scalar("SELECT count(*) FROM event_consumptions") == 0  # rolled back with the handler
        with running_worker(isolated_db, "all", tmp_path, launcher=LAUNCHER, extra_env=_replay_env(tmp_path)):
            wait_until(lambda: _settled(p), timeout=60)
        _assert_normalized_exactly_once(p)
