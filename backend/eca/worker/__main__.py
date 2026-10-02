"""Production worker entry point: ``python -m eca.worker``.

Uses the worker role (``API_WORKER_DATABASE_URL``), the default registry and ``WorkerConfig()``.
It refuses to start when ``ECA_TEST_CRASH_POINT`` is set in production or names an unknown point.
"""

from __future__ import annotations

import asyncio

from eca.platform.config import get_settings
from eca.platform.logging import configure_logging
from eca.platform.runtime import ensure_selector_event_loop_policy
from eca.worker import WorkerConfig, build_default_registry, run_worker


def main() -> None:
    settings = get_settings()
    configure_logging(settings.api_log_level)
    ensure_selector_event_loop_policy()
    asyncio.run(run_worker(registry=build_default_registry(), config=WorkerConfig(), settings=settings))


if __name__ == "__main__":
    main()
