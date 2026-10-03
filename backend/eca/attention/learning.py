"""Per-user priority learning (TECHNICAL_DESIGN.md §12.7-§12.8): fitted, bounded multipliers.

``config_for_user`` applies the user's multipliers (``user_priority_weights``) to the configured
weights; with no fitted row the configuration is unchanged. ``fit_user`` refits from the user's
``priority_pairs`` (last 180 days, at most 500, at least 10) with the bounded routine of
``fitting``; the nightly ``priority_fit`` task runs it for every active user.
"""

from __future__ import annotations

import dataclasses
import datetime

import structlog
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from eca.attention.fitting import USER_BOUNDS, Fit, Pair, effective_weights, fit_multipliers
from eca.attention.models import priority_pairs_table, user_priority_weights_table
from eca.attention.priority import PriorityConfig
from eca.identity import list_active_user_ids
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger("eca.attention.learning")

PAIR_WINDOW = datetime.timedelta(days=180)
MAX_PAIRS = 500
MIN_PAIRS = 10


async def multipliers_of(uow: UnitOfWork) -> dict[str, float]:
    w = user_priority_weights_table
    row = (
        await uow.session.execute(select(w.c.multipliers).where(w.c.user_id == uow.user_id))
    ).scalar_one_or_none()
    if not isinstance(row, dict):
        return {}
    lo, hi = USER_BOUNDS
    return {str(k): min(max(float(v), lo), hi) for k, v in row.items()}


async def config_for_user(uow: UnitOfWork, cfg: PriorityConfig) -> PriorityConfig:
    """The configured weights times the user's multipliers, renormalized (same formula, same
    templates; an override still wins)."""
    if uow.user_id is None:
        return cfg
    multipliers = await multipliers_of(uow)
    if not multipliers:
        return cfg
    return dataclasses.replace(cfg, weights=effective_weights(cfg.weights, multipliers))


async def fit_user(uow: UnitOfWork, cfg: PriorityConfig, *, now: datetime.datetime) -> Fit | None:
    p = priority_pairs_table
    rows = (
        await uow.session.execute(
            select(p.c.preferred_features, p.c.other_features)
            .where(p.c.user_id == uow.user_id, p.c.created_at >= now - PAIR_WINDOW)
            .order_by(p.c.created_at.desc(), p.c.id)
            .limit(MAX_PAIRS)
        )
    ).all()
    w = user_priority_weights_table
    if len(rows) < MIN_PAIRS:
        await uow.session.execute(delete(w).where(w.c.user_id == uow.user_id))
        return None
    pairs = [Pair(dict(r.preferred_features), dict(r.other_features)) for r in rows]
    fit = fit_multipliers(cfg.weights, pairs, bounds=USER_BOUNDS)
    values = {
        "multipliers": fit.multipliers,
        "pairs_used": fit.pairs,
        "agreement": fit.agreement,
        "config_version": cfg.version,
        "fitted_at": now,
    }
    await uow.session.execute(
        insert(w)
        .values(user_id=uow.user_id, **values)
        .on_conflict_do_update(index_elements=["user_id"], set_=values)
    )
    return fit


async def fit_all(factory: UnitOfWorkFactory, cfg: PriorityConfig, *, now: datetime.datetime) -> int:
    async with factory(user_id=None) as uow:
        users = await list_active_user_ids(uow)
    fitted = 0
    for user_id in users:
        async with factory(user_id=user_id) as uow:
            fitted += (await fit_user(uow, cfg, now=now)) is not None
    log.info("priority_fit", users=len(users), fitted=fitted)
    return fitted
