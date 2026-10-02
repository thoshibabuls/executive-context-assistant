"""Production composition of the worker: registry, resources, reconcile hooks, periodic tasks.

Domain modules register their event types and handlers when imported; they are imported here
explicitly (never by scanning). The production entry point has no hook for loading other modules.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import eca.communication
import eca.ingestion
import eca.intelligence
from eca.platform.clock import Clock, SystemClock
from eca.platform.config import Settings
from eca.platform.events import EventRegistry, Resources, default_registry
from eca.platform.jobs import PeriodicTaskSpec
from eca.platform.uow import UnitOfWorkFactory
from eca.worker.runner import ReconcileHook

_DOMAIN_MODULES = (eca.ingestion, eca.communication)  # imported for their handler registrations


def production_registry() -> EventRegistry:
    assert all(m.__name__.startswith("eca.") for m in _DOMAIN_MODULES)
    return default_registry


def production_resources(settings: Settings) -> Callable[[UnitOfWorkFactory], Resources]:
    """Real providers only. Batch A has no real connector (Gmail is slice 1.5)."""

    def build(uow_factory: UnitOfWorkFactory) -> Resources:
        return Resources.of(eca.ingestion.ConnectorRegistry(), SystemClock())

    return build


def production_reconcile_hooks() -> Sequence[ReconcileHook]:
    clock: Clock = SystemClock()

    async def stage_scan(uow_factory: UnitOfWorkFactory) -> object:
        return await eca.ingestion.reconcile_stages(uow_factory, now=clock.now())

    return (stage_scan,)


def production_periodic_tasks() -> list[PeriodicTaskSpec]:
    return [*eca.intelligence.periodic_tasks(), *eca.ingestion.periodic_tasks()]
