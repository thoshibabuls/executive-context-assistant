"""Slice 0.3 units without a database.

Registry, envelope, IDs, backoff, retry strategy, crash points, worker configuration.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from procrastinate.jobs import Job
from pydantic import BaseModel, ValidationError

from eca.platform import crashpoints
from eca.platform.dispatch import DispatchPolicy, dispatch_backoff_s
from eca.platform.errors import AuthRevoked, Gone, ValidationFailed
from eca.platform.events import EventEnvelope, EventRegistry, HandlerContext, UnregisteredEventType
from eca.platform.ids import uuid7
from eca.platform.jobs import HandlerRetryStrategy, handler_task_name, job_lock_key
from eca.worker import WorkerConfig

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Payload(BaseModel):
    value: int


async def _noop(ctx: HandlerContext) -> None:
    return None


# ---------------------------------------------------------------- registry


def test_registry_rules() -> None:
    registry = EventRegistry()
    registry.register_event("SomethingHappened", Payload)
    with pytest.raises(ValueError, match="already registered"):
        registry.register_event("SomethingHappened", Payload)
    with pytest.raises(ValueError, match="Invalid event type"):
        registry.register_event("bad type!", Payload)
    with pytest.raises(UnregisteredEventType):
        registry.handles("NotRegistered", name="h_one")
    with pytest.raises(ValueError, match="Unknown queue"):
        registry.handles("SomethingHappened", name="h_one", queue="nope")
    with pytest.raises(ValueError, match="Invalid handler name"):
        registry.handles("SomethingHappened", name="Bad Name")
    registry.handles("SomethingHappened", name="zeta_handler")(_noop)
    registry.handles("SomethingHappened", name="alpha_handler", queue="schedule")(_noop)
    with pytest.raises(ValueError, match="Handler name already registered"):
        registry.handles("SomethingHappened", name="alpha_handler")(_noop)
    assert [h.name for h in registry.handlers_for("SomethingHappened")] == ["alpha_handler", "zeta_handler"]
    assert registry.handler("alpha_handler").queue == "schedule"
    with pytest.raises(UnregisteredEventType):
        registry.handlers_for("Unknown")


def test_registries_are_isolated_from_the_default_registry() -> None:
    from eca.platform.events import default_registry

    local = EventRegistry()
    local.register_event("test.LocalOnly", Payload)
    local.handles("test.LocalOnly", name="test.local_only_handler")(_noop)
    assert not default_registry.is_registered("test.LocalOnly")
    # Domain modules register production handlers in the default registry (since Phase 1); none
    # of a local registry's handlers may appear there.
    assert "test.local_only_handler" not in {h.name for h in default_registry.handlers()}
    assert [h.name for h in local.handlers()] == ["test.local_only_handler"]


def test_envelope_validation() -> None:
    envelope = EventEnvelope(
        id=uuid7(),
        event_type="X",
        user_id=None,
        aggregate_type="t",
        aggregate_id=uuid7(),
        payload={"value": 1},
        correlation={},
        created_at=datetime.datetime.now(datetime.UTC),
    )
    assert EventEnvelope.model_validate(envelope.model_dump(mode="json")) == envelope
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({**envelope.model_dump(mode="json"), "extra": "content"})


# ---------------------------------------------------------------- identifiers and keys


def test_uuid7_layout_and_order() -> None:
    first = uuid7(unix_ms=1_700_000_000_000)
    later = uuid7(unix_ms=1_700_000_000_001)
    assert first.version == 7 and first.variant == uuid.RFC_4122
    assert first.int >> 80 == 1_700_000_000_000
    assert first < later
    assert len({uuid7() for _ in range(1000)}) == 1000


def test_lock_key_and_task_name_format() -> None:
    event_id = uuid.UUID("01890000-0000-7000-8000-000000000001")
    assert (
        job_lock_key("recompute_priority", event_id)
        == "recompute_priority:01890000-0000-7000-8000-000000000001"
    )
    assert handler_task_name("recompute_priority") == "eca.handler.recompute_priority"


# ---------------------------------------------------------------- backoff and retry


@pytest.mark.parametrize(
    "failures, expected", [(1, 1.5), (2, 2.5), (3, 4.5), (9, 256.5), (10, 300.0), (20, 300.0)]
)
def test_dispatch_backoff(failures: int, expected: float) -> None:
    assert dispatch_backoff_s(failures, DispatchPolicy(), rand=lambda: 0.5) == expected


def test_dispatch_backoff_jitter_bounds() -> None:
    assert dispatch_backoff_s(1, DispatchPolicy(), rand=lambda: 0.0) == 1.0
    assert dispatch_backoff_s(1, DispatchPolicy(), rand=lambda: 0.999) == pytest.approx(1.999)
    with pytest.raises(ValueError):
        dispatch_backoff_s(0, DispatchPolicy())


NOW = datetime.datetime(2026, 10, 2, 12, 0, tzinfo=datetime.UTC)


def _job(attempts: int) -> Job:
    return Job(
        id=7,
        queue="events",
        lock="h:e",
        queueing_lock="h:e",
        task_name="eca.handler.h",
        task_kwargs={"envelope": {"id": "e"}},
        attempts=attempts,
    )


def _strategy(rand: float = 0.5) -> HandlerRetryStrategy:
    return HandlerRetryStrategy(clock=lambda: NOW, rand=lambda: rand)


@pytest.mark.parametrize("attempts, delay", [(0, 5.5), (1, 10.5), (2, 20.5), (5, 160.5), (6, 320.5)])
def test_handler_retry_delays(attempts: int, delay: float) -> None:
    decision = _strategy().get_retry_decision(exception=RuntimeError("x"), job=_job(attempts))
    assert decision is not None and decision.retry_at == NOW + datetime.timedelta(seconds=delay)


def test_handler_retry_stops_after_eight_runs() -> None:
    assert _strategy().get_retry_decision(exception=RuntimeError("x"), job=_job(6)) is not None  # run 7
    assert _strategy().get_retry_decision(exception=RuntimeError("x"), job=_job(7)) is None  # run 8: dead


def test_handler_retry_cap_and_jitter() -> None:
    strategy = HandlerRetryStrategy(
        base_s=5, max_delay_s=600, max_attempts=20, clock=lambda: NOW, rand=lambda: 0.99
    )
    assert strategy.delay_s(1) == pytest.approx(5.99)
    assert strategy.delay_s(8) == 600.0  # 5 * 2^7 = 640 is capped


def test_handler_retry_honours_a_provider_retry_after() -> None:
    from eca.platform.errors import RateLimited

    decision = _strategy().get_retry_decision(exception=RateLimited("r", retry_after_s=90), job=_job(0))
    assert decision is not None and decision.retry_at == NOW + datetime.timedelta(seconds=90)
    capped = _strategy().get_retry_decision(exception=RateLimited("r", retry_after_s=86_400), job=_job(0))
    assert capped is not None and capped.retry_at == NOW + datetime.timedelta(seconds=3600)
    plain = _strategy().get_retry_decision(exception=RateLimited("r"), job=_job(1))
    assert plain is not None and plain.retry_at == NOW + datetime.timedelta(seconds=10.5)


@pytest.mark.parametrize("exc", [ValidationFailed("v"), AuthRevoked("a"), Gone("g")])
def test_handler_retry_non_retryable_errors(exc: Exception) -> None:
    assert _strategy().get_retry_decision(exception=exc, job=_job(0)) is None


# ---------------------------------------------------------------- crash points


def test_crash_points_inert_when_unset() -> None:
    assert crashpoints.configure(is_production=False, environ={}) is None
    crashpoints.hit("dispatch.after_claim")  # returns: nothing armed


def test_crash_point_unknown_name_is_a_startup_error() -> None:
    with pytest.raises(crashpoints.CrashPointConfigError, match="not a known crash point"):
        crashpoints.configure(is_production=False, environ={crashpoints.ENV_VAR: "dispatch.nowhere"})


def test_crash_point_in_production_is_a_startup_error() -> None:
    with pytest.raises(crashpoints.CrashPointConfigError, match="production"):
        crashpoints.configure(is_production=True, environ={crashpoints.ENV_VAR: "handler.before_commit"})


def test_crash_point_set_names() -> None:
    assert {
        "dispatch.after_claim",
        "dispatch.after_defer",
        "dispatch.before_commit",
        "handler.after_consumption",
        "handler.before_commit",
        "handler.after_commit",
    } == crashpoints.CRASH_POINTS
    assert crashpoints.EXIT_CODE == 97


def test_armed_crash_point_exits_with_97() -> None:
    code = (
        "from eca.platform import crashpoints as c;"
        "c.configure(is_production=False, environ={'ECA_TEST_CRASH_POINT': 'handler.before_commit'});"
        "c.hit('handler.after_commit');"
        "c.hit('handler.before_commit');"
        "raise SystemExit(0)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND_DIR, check=False)
    assert result.returncode == 97


def _run_production_worker(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("API_", "ECA_"))}
    return subprocess.run(
        [sys.executable, "-m", "eca.worker"],
        cwd=BACKEND_DIR,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    "env, message",
    [
        (
            {"API_ENV": "production", "ECA_TEST_CRASH_POINT": "handler.before_commit"},
            "must not be set in production",
        ),
        ({"API_ENV": "test", "ECA_TEST_CRASH_POINT": "nowhere"}, "not a known crash point"),
    ],
)
def test_production_entry_point_refuses_crash_configuration(env: dict[str, str], message: str) -> None:
    result = _run_production_worker(
        {**env, "API_WORKER_DATABASE_URL": "postgresql://nobody:x@127.0.0.1:1/none"}
    )
    assert result.returncode != 0
    assert "CrashPointConfigError" in result.stderr and message in result.stderr


def test_production_worker_does_not_load_test_modules() -> None:
    code = (
        "import sys, eca.worker.__main__; print(sorted(m for m in sys.modules if m.split('.')[0] == 'tests'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND_DIR, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


# ---------------------------------------------------------------- worker config


def test_worker_config_defaults_follow_the_design() -> None:
    config = WorkerConfig()
    assert dict(config.queues) == {
        "events": 8,
        "sync": 4,
        "ingest": 8,
        "extract": 8,
        "apply": 8,
        "embed": 2,
        "ai_standard": 4,
        "media": 1,
        "schedule": 1,
    }
    assert config.dispatch == DispatchPolicy(
        batch_size=100, tick_s=1.0, backoff_base_s=1.0, backoff_max_s=300.0, max_attempts=10
    )
    retry: Any = config.handler_retry
    assert (retry.base_s, retry.max_delay_s, retry.max_attempts) == (5.0, 600.0, 8)
    assert (config.heartbeat_interval_s, config.stalled_timeout_s, config.reconcile_min_age_s) == (
        10.0,
        30.0,
        60.0,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"queues": {"nope": 1}},
        {"queues": {"events": 0}},
        {"heartbeat_interval_s": 30.0, "stalled_timeout_s": 30.0},
    ],
)
def test_worker_config_rejects_invalid_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        WorkerConfig(**kwargs)


def test_worker_settings() -> None:
    from eca.platform.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (settings.api_db_runtime_role, settings.api_db_worker_role) == ("eca_app", "eca_worker")
    assert settings.api_worker_database_url is None
