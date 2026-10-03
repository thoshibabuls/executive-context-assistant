"""Preference pairs from priority overrides (TECHNICAL_DESIGN.md §12.8; ``priority_pairs`` schema
BACKEND_DESIGN.md §17.6).

When the user pins an item or thread high (``1``), it is preferred over the 3 open items or
conversations whose computed score is nearest above its own; pinned low (``-1``), the 3 nearest
below are preferred over it. Feature vectors are snapshotted (numbers only), so the nightly fit
never recomputes history. Clearing an override records nothing.
"""

from __future__ import annotations

import datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert

from eca.attention.learning import config_for_user
from eca.attention.models import priority_pairs_table
from eca.attention.priority import PriorityConfig
from eca.attention.service import Scored, scored_conversations, scored_items
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork

NEIGHBOURS = 3


def neighbours(target: Scored, others: list[Scored], override: int) -> list[Scored]:
    """The nearest computed scores above (override 1) or below (override -1) the target."""
    if override == 1:
        above = [o for o in others if o.computed > target.computed]
        return sorted(above, key=lambda o: (o.computed, str(o.id)))[:NEIGHBOURS]
    below = [o for o in others if o.computed < target.computed]
    return sorted(below, key=lambda o: (-o.computed, str(o.id)))[:NEIGHBOURS]


async def record_override_pairs(
    uow: UnitOfWork,
    cfg: PriorityConfig,
    *,
    entity_type: str,
    entity_id: UUID,
    override: int | None,
    now: datetime.datetime,
) -> int:
    if override not in (1, -1):
        return 0
    cfg = await config_for_user(uow, cfg)
    pool = await scored_items(uow, cfg, now=now) + await scored_conversations(uow, cfg, now=now)
    target = next((s for s in pool if s.entity_type == entity_type and s.id == entity_id), None)
    if target is None:
        return 0
    others = [s for s in pool if s is not target]
    written = 0
    for other in neighbours(target, others, override):
        preferred, rest = (target, other) if override == 1 else (other, target)
        result = await uow.session.execute(
            insert(priority_pairs_table)
            .values(
                id=uuid7(),
                user_id=uow.user_id,
                preferred_type=preferred.entity_type,
                preferred_id=preferred.id,
                other_type=rest.entity_type,
                other_id=rest.id,
                preferred_features=dict(preferred.features.values),
                other_features=dict(rest.features.values),
                source="override_up" if override == 1 else "override_down",
                config_version=cfg.version,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "preferred_type", "preferred_id", "other_type", "other_id"]
            )
        )
        written += int(result.rowcount)  # type: ignore[attr-defined]
    return written
