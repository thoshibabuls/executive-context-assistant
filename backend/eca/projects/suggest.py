"""Nightly project suggestions (CONTEXT_ARCHITECTURE.md §11.8, §12.5; BACKEND_DESIGN.md §15).

Per user: ``project_hint`` values of live items and decisions are normalized (lower case,
alphanumeric words); a hint shared by at least 3 distinct sources that no project has used yet
becomes a suggested project (``origin = ai``, ``verification_status = suggested``), named by its
most frequent spelling. Hints are grouped by exact normalized key in the MVP (no fuzzy
clustering). A rejected suggestion keeps its ``hint_key``, so it is never suggested again.
Computed members of confirmed projects are refreshed from their matched items.
"""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from dataclasses import dataclass
from uuid import UUID

import structlog

from eca import work
from eca.identity import list_active_user_ids
from eca.people import get_self_person
from eca.platform.jobs import PeriodicTaskSpec
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory
from eca.projects.service import (
    active_confirmed,
    existing_hint_keys,
    insert_suggestion,
    normalize_hint,
    set_computed_members,
)

log = structlog.get_logger("eca.projects.suggest")

PROJECT_SUGGEST_TASK = "eca.projects.suggest"
MIN_SOURCES = 3
MAX_COMPUTED_MEMBERS = 10


@dataclass
class SuggestReport:
    suggested: int = 0
    member_updates: int = 0


async def suggest_user(uow: UnitOfWork, *, self_id: UUID | None = None) -> SuggestReport:
    report = SuggestReport()
    spellings: dict[str, Counter[str]] = defaultdict(Counter)
    sources: dict[str, set[UUID]] = defaultdict(set)
    for hs in await work.project_hint_sources(uow):
        key = normalize_hint(hs.hint)
        if len(key) < 3:
            continue
        spellings[key][hs.hint.strip()] += 1
        sources[key].add(hs.source_item_id)
    used_keys, names = await existing_hint_keys(uow)
    for key in sorted(sources):
        if len(sources[key]) < MIN_SOURCES or key in used_keys or key in names:
            continue
        name = sorted(spellings[key].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        if await insert_suggestion(uow, name=name, hint_key=key, sources=len(sources[key])) is not None:
            report.suggested += 1
    for project in await active_confirmed(uow):
        items = await work.items_for_project(
            uow, project_id=project.id, names=project.names, include_closed=True
        )
        counts: Counter[UUID] = Counter(
            p
            for i in items
            for p in (i.owner_person_id, i.counterparty_person_id)
            if p is not None and p != self_id
        )
        top = [
            p for p, _ in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[:MAX_COMPUTED_MEMBERS]
        ]
        await set_computed_members(uow, project.id, top)
        report.member_updates += 1
    return report


async def suggest_all(uow_factory: UnitOfWorkFactory, *, now: datetime.datetime) -> SuggestReport:
    async with uow_factory(user_id=None) as uow:
        users = await list_active_user_ids(uow)
    total = SuggestReport()
    for user_id in users:
        async with uow_factory(user_id=user_id) as uow:
            self_p = await get_self_person(uow)
            r = await suggest_user(uow, self_id=self_p.id)
        total.suggested += r.suggested
        total.member_updates += r.member_updates
    log.info("project_suggest", users=len(users), suggested=total.suggested, at=now.isoformat())
    return total


async def _run(uow_factory: UnitOfWorkFactory, now: datetime.datetime) -> None:
    await suggest_all(uow_factory, now=now)


def periodic_tasks() -> list[PeriodicTaskSpec]:
    return [
        PeriodicTaskSpec(
            name=PROJECT_SUGGEST_TASK,
            periodic_id="project_suggest",
            cron="40 2 * * *",
            queue="schedule",
            run=_run,
        )
    ]
