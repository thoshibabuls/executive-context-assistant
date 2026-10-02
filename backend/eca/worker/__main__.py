"""Production worker entry point: ``python -m eca.worker``.

Uses the worker role (``API_WORKER_DATABASE_URL``), ``WorkerConfig()`` and the production
composition (``eca.worker.composition``): the domain handlers, their resources, the source-item
stage scan and the modules' periodic tasks.
It refuses to start when ``ECA_TEST_CRASH_POINT`` is set in production or names an unknown point.
"""

from __future__ import annotations

import asyncio

from eca.platform.config import get_settings
from eca.platform.logging import configure_logging
from eca.platform.runtime import ensure_selector_event_loop_policy
from eca.worker import WorkerConfig, run_worker
from eca.worker.composition import (
    production_periodic_tasks,
    production_reconcile_hooks,
    production_registry,
    production_resources,
)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.api_log_level)
    ensure_selector_event_loop_policy()
    asyncio.run(
        run_worker(
            registry=production_registry(),
            config=WorkerConfig(),
            settings=settings,
            periodic=production_periodic_tasks(),
            build_resources=production_resources(settings),
            reconcile_hooks=production_reconcile_hooks(),
        )
    )


if __name__ == "__main__":
    main()
