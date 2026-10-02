"""Test-only launcher for pipeline-level crash tests (RT-01 pipeline level, RT-02, RT-02b).

Runs the **production composition** (``eca.worker.composition``: real domain handlers and
resources from settings, e.g. ``API_AI_MODE=replay`` with a cassette directory) with the fast
test timings of ``worker_main``. Only the timings differ from ``python -m eca.worker``.
"""

from __future__ import annotations

import argparse
import asyncio

from eca.platform.config import Settings
from eca.platform.logging import configure_logging
from eca.platform.runtime import ensure_selector_event_loop_policy
from eca.worker import run_worker
from eca.worker.composition import production_registry, production_resources
from tests.reliability.support.worker_main import fast_config


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
            registry=production_registry(),
            config=fast_config(args.batch_size),
            settings=settings,
            mode=args.mode,
            build_resources=production_resources(settings),
        )
    )


if __name__ == "__main__":
    main()
