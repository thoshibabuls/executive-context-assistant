"""Per-user daily AI budget caps (AI_COST_MODEL.md §7, §7.1).

Spend is read from the user's ``ai_cost_rollups`` since 00:00 UTC: the API role may read its own
roll-ups (``ai_cost_rollups_api_read``) and the worker role reads them in the user's transaction,
always with an explicit ``user_id`` predicate. Roll-ups lag by up to 15 minutes.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select

from eca.intelligence.models import ai_cost_rollups_table
from eca.platform.uow import UnitOfWork

DAILY_SOFT_CAP_USD = Decimal("1.00")
DAILY_HARD_CAP_USD = Decimal("2.50")


class BudgetLevel(StrEnum):
    OK = "ok"
    SOFT = "soft_cap"  # AI-07 replaced by deterministic answers (+ AI-06 where possible)
    HARD = "hard_cap"  # chat deterministic only; indexing without embeddings


def level_for(spend: Decimal) -> BudgetLevel:
    if spend >= DAILY_HARD_CAP_USD:
        return BudgetLevel.HARD
    if spend >= DAILY_SOFT_CAP_USD:
        return BudgetLevel.SOFT
    return BudgetLevel.OK


async def daily_spend(uow: UnitOfWork, *, now: datetime.datetime) -> Decimal:
    if uow.user_id is None:
        return Decimal(0)
    day_start = now.astimezone(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    r = ai_cost_rollups_table
    total = (
        await uow.session.execute(
            select(func.coalesce(func.sum(r.c.est_cost_usd), 0)).where(
                r.c.user_id == uow.user_id, r.c.bucket_start >= day_start
            )
        )
    ).scalar_one()
    return Decimal(str(total))


async def budget_level(uow: UnitOfWork, *, now: datetime.datetime) -> BudgetLevel:
    return level_for(await daily_spend(uow, now=now))
