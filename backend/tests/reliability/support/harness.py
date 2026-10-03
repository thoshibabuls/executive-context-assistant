"""Process and database helpers for the subprocess crash tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from sqlalchemy import text

from eca.platform import crashpoints
from eca.platform.db import create_engine, create_session_factory
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWorkFactory
from tests.conftest import RUNTIME_ROLE, WORKER_ROLE, TempDatabase
from tests.reliability.support.synthetic import SyntheticPayload, build_registry

BACKEND_DIR = Path(__file__).resolve().parents[3]

# On Windows, SIGTERM is TerminateProcess (exit code 1, no cleanup). A worker started in its own
# process group stops gracefully on CTRL_BREAK_EVENT instead (eca.worker.runner).
if sys.platform == "win32":
    _GRACEFUL_STOP = signal.CTRL_BREAK_EVENT
    _POPEN_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    _GRACEFUL_STOP = signal.SIGTERM
    _POPEN_FLAGS = 0


@dataclass
class WorkerProcess:
    proc: subprocess.Popen[bytes]
    log_path: Path
    _closed: bool = field(default=False)

    def wait_exit(self, timeout: float = 30.0) -> int:
        return self.proc.wait(timeout=timeout)

    def stop(self, timeout: float = 20.0) -> int:
        """Graceful stop (SIGTERM; CTRL_BREAK_EVENT on Windows); kill if it does not end in time."""
        if self.proc.poll() is None:
            self.proc.send_signal(_GRACEFUL_STOP)
            try:
                return self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                return self.proc.wait(timeout=timeout)
        return self.proc.returncode

    def logs(self) -> list[dict[str, Any]]:
        entries = []
        for line in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("{"):
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def events(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.logs() if e.get("event") == name]


def start_worker(
    db: TempDatabase,
    mode: str,
    log_dir: Path,
    *,
    crash: str | None = None,
    batch_size: int = 100,
    launcher: str = "tests.reliability.support.worker_main",
    extra_env: dict[str, str] | None = None,
) -> WorkerProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("API_", "ECA_TEST_CRASH"))}
    env.update(
        {
            "API_ENV": "test",
            "API_WORKER_DATABASE_URL": db.worker_url,
            "API_DB_WORKER_ROLE": WORKER_ROLE,
            "API_DB_RUNTIME_ROLE": RUNTIME_ROLE,
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.update(extra_env or {})
    if crash is not None:
        env[crashpoints.ENV_VAR] = crash
    log_path = log_dir / f"worker-{mode}-{time.monotonic_ns()}.log"
    with log_path.open("wb") as out:
        proc = subprocess.Popen(  # test-only launcher with fixed arguments
            [
                sys.executable,
                "-m",
                launcher,
                "--mode",
                mode,
                "--batch-size",
                str(batch_size),
            ],
            cwd=BACKEND_DIR,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=_POPEN_FLAGS,
        )
    return WorkerProcess(proc=proc, log_path=log_path)


@contextmanager
def running_worker(
    db: TempDatabase,
    mode: str,
    log_dir: Path,
    *,
    batch_size: int = 100,
    launcher: str = "tests.reliability.support.worker_main",
    extra_env: dict[str, str] | None = None,
) -> Iterator[WorkerProcess]:
    worker = start_worker(db, mode, log_dir, batch_size=batch_size, launcher=launcher, extra_env=extra_env)
    try:
        yield worker
    finally:
        worker.stop()


def rows(db: TempDatabase, query: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(db.admin_url) as conn:
        return conn.execute(query, params).fetchall()  # type: ignore[arg-type]


def scalar(db: TempDatabase, query: str, params: Sequence[Any] = ()) -> Any:
    result = rows(db, query, params)
    return result[0][0] if result else None


def wait_until(predicate: Callable[[], bool], *, timeout: float = 30.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def effects(db: TempDatabase, handler: str | None = None) -> int:
    if handler is None:
        return int(scalar(db, "SELECT count(*) FROM rt_effects"))
    return int(scalar(db, "SELECT count(*) FROM rt_effects WHERE handler = %s", (handler,)))


def jobs_settled(db: TempDatabase) -> bool:
    """No handler job is waiting or running."""
    return not scalar(
        db,
        "SELECT count(*) FROM procrastinate_jobs WHERE task_name LIKE 'eca.handler.%%' "
        "AND status IN ('todo', 'doing')",
    )


def outbox_status(db: TempDatabase, event_id: UUID) -> str:
    return str(scalar(db, "SELECT status FROM outbox WHERE id = %s", (event_id,)))


async def publish_as_api(
    db: TempDatabase,
    event_type: str,
    *,
    user_id: UUID,
    count: int = 1,
    rollback: bool = False,
    business_note: str | None = None,
) -> list[UUID]:
    """Business row (optional) + events in ONE API-role transaction; rolled back on request."""
    registry = build_registry()
    engine = create_engine(db.runtime_url, pool_size=1, max_overflow=0)
    ids: list[UUID] = []
    try:
        uow_factory = UnitOfWorkFactory(create_session_factory(engine))
        try:
            async with uow_factory(user_id=user_id) as uow:
                if business_note is not None:
                    await uow.session.execute(
                        text("INSERT INTO rt_business (id, user_id, note) VALUES (:id, :uid, :note)"),
                        {"id": uuid7(), "uid": user_id, "note": business_note},
                    )
                for i in range(count):
                    event = NewEvent(
                        event_type=event_type,
                        aggregate_type="test",
                        aggregate_id=uuid7(),
                        payload=SyntheticPayload(marker=f"m{i}"),
                    )
                    ids.append(await publish(uow, event, registry=registry))
                if rollback:
                    raise _Rollback
        except _Rollback:
            pass
    finally:
        await engine.dispose()
    return ids


class _Rollback(Exception):
    pass
