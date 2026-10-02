"""Test-only worker launcher: ``python -m tests.reliability.support.worker_main --mode ...``.

Registers the synthetic handlers in their own registry and calls ``eca.worker.run_worker`` with
fast timings. Configuration comes from the environment set by the test harness
(``API_WORKER_DATABASE_URL``, role names, ``ECA_TEST_CRASH_POINT``). The production entry point
``python -m eca.worker`` never loads this module.
"""

from __future__ import annotations

import argparse
import asyncio

from eca.platform.config import Settings
from eca.platform.dispatch import DispatchPolicy
from eca.platform.jobs import HandlerRetryStrategy
from eca.platform.logging import configure_logging
from eca.platform.runtime import ensure_selector_event_loop_policy
from eca.worker import WorkerConfig, run_worker
from tests.reliability.support.synthetic import build_registry


def fast_config(batch_size: int) -> WorkerConfig:
    return WorkerConfig(
        dispatch=DispatchPolicy(batch_size=batch_size, tick_s=0.1),
        handler_retry=HandlerRetryStrategy(base_s=0.05, max_delay_s=1.0),
        heartbeat_interval_s=0.5,
        stalled_timeout_s=2.0,
        fetch_job_polling_interval_s=0.1,
        shutdown_graceful_timeout_s=5.0,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["all", "dispatcher", "jobs"], required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    configure_logging("INFO")
    ensure_selector_event_loop_policy()
    asyncio.run(
        run_worker(
            registry=build_registry(), config=fast_config(args.batch_size), settings=settings, mode=args.mode
        )
    )


if __name__ == "__main__":
    main()
