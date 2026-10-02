"""Name lookup for questions ("What did John promise?"; CONTEXT_ARCHITECTURE.md §10.2, §9.8 step 1).

Candidates come from the user's name aliases (exact alias, exact first name, trigram similarity)
and email identifiers. Persons are never merged here: two Johns are two candidates, and the
caller asks which one is meant or states the choice (CC-18, CC-42).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select

from eca.people.identity_rules import normalize_alias
from eca.people.models import person_identifiers_table, persons_table
from eca.people.service import PersonRef, _person, find_by_email
from eca.platform.uow import UnitOfWork

EXACT = 1.0
FIRST_NAME = 0.8
TRIGRAM_MIN = 0.6
TRIGRAM_WEIGHT = 0.9


@dataclass(frozen=True)
class NameMatch:
    person: PersonRef
    score: float
    alias: str


async def match_names(uow: UnitOfWork, name: str, *, limit: int = 5) -> list[NameMatch]:
    """Persons a name may refer to, best first (the user's own Person and merged persons excluded)."""
    alias = normalize_alias(name)
    if not alias:
        return []
    if "@" in alias:
        found = await find_by_email(uow, alias)
        return [NameMatch(found, EXACT, alias)] if found is not None and not found.is_self else []
    ids, p = person_identifiers_table, persons_table
    similarity = func.similarity(ids.c.value_normalized, alias)
    rows = await uow.session.execute(
        select(ids.c.person_id, ids.c.value_normalized, similarity.label("sim"))
        .join(p, p.c.id == ids.c.person_id)
        .where(
            ids.c.user_id == uow.user_id,
            ids.c.kind == "name_alias",
            ~p.c.is_self,
            p.c.merged_into_id.is_(None),
            p.c.deleted_at.is_(None),
            or_(
                ids.c.value_normalized == alias,
                func.split_part(ids.c.value_normalized, " ", 1) == alias,
                similarity >= TRIGRAM_MIN,
            ),
        )
    )
    best: dict[UUID, tuple[float, str]] = {}
    for r in rows:
        value = r.value_normalized
        if value == alias:
            s = EXACT
        elif value.split(" ", 1)[0] == alias:
            s = FIRST_NAME
        else:
            s = float(r.sim) * TRIGRAM_WEIGHT
        if r.person_id not in best or s > best[r.person_id][0]:
            best[r.person_id] = (s, value)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], str(kv[0])))[:limit]
    return [NameMatch(await _person(uow, pid), score, value) for pid, (score, value) in ranked]
