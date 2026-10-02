"""Worker composition package (BACKEND_DESIGN.md §4.2, §5.3): ``python -m eca.worker``.

Composes the platform's dispatcher, Procrastinate queue workers and infrastructure tasks with
the handlers registered by domain modules. Like ``eca.api``, nothing imports it.
"""

from __future__ import annotations

from eca.platform.events import EventRegistry, default_registry
from eca.worker.config import WorkerConfig
from eca.worker.runner import Mode, WorkerStartupError, run_worker


def build_default_registry() -> EventRegistry:
    """The production registry: event types and handlers of the domain modules.

    Domain modules register their handlers at import time; they are imported here explicitly.
    Slice 0.3 has no domain handlers yet.
    """
    return default_registry


__all__ = ["Mode", "WorkerConfig", "WorkerStartupError", "build_default_registry", "run_worker"]
