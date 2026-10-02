"""Production composition of the worker: registry, resources, reconcile hooks, periodic tasks.

Domain modules register their event types and handlers when imported; they are imported here
explicitly (never by scanning). The production entry point has no hook for loading other modules.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import httpx

import eca.attention
import eca.communication
import eca.connections
import eca.ingestion
import eca.intelligence
import eca.meetings
import eca.privacy
import eca.retrieval
import eca.work
from eca.platform.clock import Clock, SystemClock
from eca.platform.config import Settings
from eca.platform.events import EventRegistry, Resources, default_registry
from eca.platform.jobs import PeriodicTaskSpec
from eca.platform.uow import UnitOfWorkFactory
from eca.worker.runner import ReconcileHook

# Imported for their handler registrations.
_DOMAIN_MODULES = (
    eca.ingestion,
    eca.communication,
    eca.meetings,
    eca.work,
    eca.attention,
    eca.privacy,
    eca.retrieval,
)


def production_registry() -> EventRegistry:
    assert all(m.__name__.startswith("eca.") for m in _DOMAIN_MODULES)
    return default_registry


def production_resources(settings: Settings) -> Callable[[UnitOfWorkFactory], Resources]:
    """AI client from settings (``API_AI_MODE``: live, replay or record), the connectors, and for
    account deletion the token crypto (when ``TOKEN_KEK`` is set) and an HTTP client for token
    revocation. The HTTP client lives as long as the worker process."""

    def build(uow_factory: UnitOfWorkFactory) -> Resources:
        ai = eca.intelligence.build_ai_client(settings, uow_factory=uow_factory)
        values: list[object] = [
            eca.ingestion.build_connector_registry(settings, uow_factory),
            SystemClock(),
            ai,
            httpx.AsyncClient(timeout=30.0),
        ]
        if settings.token_kek is not None:
            values.append(
                eca.connections.TokenCrypto(
                    settings.token_kek.get_secret_value(), version=settings.token_kek_version
                )
            )
        return Resources.of(*values)

    return build


def production_reconcile_hooks() -> Sequence[ReconcileHook]:
    clock: Clock = SystemClock()

    async def stage_scan(uow_factory: UnitOfWorkFactory) -> object:
        return await eca.ingestion.reconcile_stages(uow_factory, now=clock.now())

    return (stage_scan,)


def production_periodic_tasks() -> list[PeriodicTaskSpec]:
    return [
        *eca.intelligence.periodic_tasks(),
        *eca.ingestion.periodic_tasks(),
        *eca.attention.periodic_tasks(),
        *eca.privacy.periodic_tasks(),
    ]
