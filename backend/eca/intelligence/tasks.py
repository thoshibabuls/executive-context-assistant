"""Periodic tasks owned by ``intelligence``, registered by ``eca.worker`` (BACKEND_DESIGN.md §5.5).

``cost_rollup`` runs every 15 minutes on the ``schedule`` queue under a lock, as the worker role.
It recomputes the recent buckets of ``ai_cost_rollups`` from ``ai_calls`` using the scheduled
tick as ``now``, so a rerun of the same tick writes the same rows, then records budget-cap events
in ``audit_log`` (AI_COST_MODEL.md §7.2; deduplicated per user, cap and day).
"""

from __future__ import annotations

import datetime

from eca.intelligence.budget import audit_budget_caps
from eca.intelligence.provider.meter import rollup_costs
from eca.platform.jobs import PeriodicTaskSpec
from eca.platform.uow import UnitOfWorkFactory

COST_ROLLUP_TASK = "eca.intelligence.cost_rollup"


async def _cost_rollup(uow_factory: UnitOfWorkFactory, now: datetime.datetime) -> None:
    await rollup_costs(uow_factory, now=now)
    await audit_budget_caps(uow_factory, now=now)


def periodic_tasks() -> list[PeriodicTaskSpec]:
    return [
        PeriodicTaskSpec(
            name=COST_ROLLUP_TASK,
            periodic_id="cost_rollup",
            cron="*/15 * * * *",
            queue="schedule",
            run=_cost_rollup,
        )
    ]
