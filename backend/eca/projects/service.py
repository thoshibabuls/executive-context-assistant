"""Projects: user-created and confirmed projects, AI-derived suggestions, hint matching
(CONTEXT_ARCHITECTURE.md §10.3, §11.8, §12.5; TECHNICAL_DESIGN.md §13.7; BACKEND_DESIGN.md §16.7).

Authority: user-created or confirmed > suggested (≥ 3 sources share a hint) > a single hint. A
suggestion stays labelled AI-derived (``origin = ai``, ``verification_status = suggested``) until
the user confirms it; confirmation, rejection and edits are authority-5 user events recorded in
``context_events`` (through ``work``) and ``feedback_events``. Matching uses pg_trgm similarity
on the name and aliases; it never merges or creates projects by itself.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert

from eca import work
from eca.platform.errors import Conflict, NotFound, PreconditionFailed, ValidationFailed
from eca.platform.feedback import record_feedback
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork
from eca.projects.models import project_members_table, projects_table

MATCH_MIN_SIMILARITY = 0.45
QUIET_AFTER = datetime.timedelta(days=30)
EDITABLE = frozenset({"name", "description", "aliases", "importance_user", "status"})
CONFIRMED = ("confirmed", "user_created")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_hint(hint: str) -> str:
    return " ".join(_NON_ALNUM.sub(" ", hint.lower()).split())


@dataclass(frozen=True)
class ProjectView:
    id: UUID
    name: str
    description: str | None
    aliases: tuple[str, ...]
    hint_key: str | None
    status: str
    importance_user: int | None
    origin: str
    verification_status: str
    suggestion_sources: int | None
    user_fields: tuple[str, ...]
    version: int
    created_at: datetime.datetime | None

    @property
    def confirmed(self) -> bool:
        return self.verification_status in CONFIRMED

    @property
    def names(self) -> list[str]:
        return [self.name, *self.aliases]


def _view(r: Any) -> ProjectView:
    return ProjectView(
        r.id,
        r.name,
        r.description,
        tuple(r.aliases or ()),
        r.hint_key,
        r.status,
        r.importance_user,
        r.origin,
        r.verification_status,
        r.suggestion_sources,
        tuple(r.user_fields or ()),
        r.version,
        r.created_at,
    )


def _live(uow: UnitOfWork) -> Any:
    p = projects_table
    return select(p).where(p.c.user_id == uow.user_id, p.c.deleted_at.is_(None))


async def get_project(uow: UnitOfWork, project_id: UUID, *, for_update: bool = False) -> ProjectView:
    stmt = _live(uow).where(projects_table.c.id == project_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await uow.session.execute(stmt)).one_or_none()
    if row is None:
        raise NotFound("project not found")
    return _view(row)


async def list_projects(
    uow: UnitOfWork, *, verification: str | None, after: list[Any] | None, limit: int
) -> tuple[list[ProjectView], list[Any] | None]:
    p = projects_table
    stmt = _live(uow)
    if verification is not None:
        stmt = stmt.where(p.c.verification_status == verification)
    else:
        stmt = stmt.where(p.c.verification_status != "rejected")
    if after is not None:
        stmt = stmt.where(tuple_(func.lower(p.c.name), p.c.id) > tuple_(str(after[0]), UUID(str(after[1]))))
    rows = (await uow.session.execute(stmt.order_by(func.lower(p.c.name), p.c.id).limit(limit + 1))).all()
    page = rows[:limit]
    next_key = [page[-1].name.lower(), str(page[-1].id)] if len(rows) > limit and page else None
    return [_view(r) for r in page], next_key


async def match_projects(
    uow: UnitOfWork, text: str, *, confirmed_only: bool = True, limit: int = 3
) -> list[tuple[ProjectView, float]]:
    """Projects whose name or an alias is trigram-similar to ``text`` (≥ 0.45), best first."""
    query = text.strip().lower()
    if not query:
        return []
    p = projects_table
    alias = func.unnest(p.c.aliases).table_valued("a").alias("project_alias")
    alias_sim = (
        select(func.max(func.similarity(func.lower(alias.c.a), query)))
        .select_from(alias)
        .scalar_subquery()
        .correlate(p)
    )
    score = func.greatest(func.similarity(func.lower(p.c.name), query), func.coalesce(alias_sim, 0.0))
    stmt = (
        _live(uow)
        .add_columns(score.label("score"))
        .where(p.c.verification_status != "rejected", p.c.status == "active", score >= MATCH_MIN_SIMILARITY)
    )
    if confirmed_only:
        stmt = stmt.where(p.c.verification_status.in_(CONFIRMED))
    rows = (await uow.session.execute(stmt.order_by(score.desc(), p.c.id).limit(limit))).all()
    return [(_view(r), float(r.score)) for r in rows]


async def alias_catalog(uow: UnitOfWork) -> list[tuple[UUID, str]]:
    """(project, name or alias) of confirmed projects, for the alias scan (§12.7: ≥ 4 characters)."""
    p = projects_table
    rows = await uow.session.execute(
        _live(uow).where(p.c.verification_status.in_(CONFIRMED), p.c.status == "active").order_by(p.c.id)
    )
    out: list[tuple[UUID, str]] = []
    for r in rows:
        for name in [r.name, *(r.aliases or ())]:
            key = " ".join(name.lower().split())
            if len(key) >= 4:
                out.append((r.id, key))
    return out


def _event_key(project_id: UUID, event_type: str, request_key: str) -> bytes:
    return hashlib.sha256(f"user:{request_key}:{event_type}:project:{project_id}".encode()).digest()


async def _user_event(
    uow: UnitOfWork,
    project_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    request_key: str,
    at: datetime.datetime,
) -> None:
    await work.record_entity_event(
        uow,
        entity_type="project",
        entity_id=project_id,
        event_type=event_type,
        actor="user",
        authority=5,
        materiality=2,
        occurred_at=at,
        dedupe_key=_event_key(project_id, event_type, request_key),
        payload=payload,
    )


def _clean_aliases(aliases: Sequence[str] | None) -> list[str]:
    out: list[str] = []
    for a in aliases or ():
        a = " ".join(a.split())
        if not a or len(a) > 120:
            raise ValidationFailed("aliases must be 1-120 characters")
        if a.lower() not in [x.lower() for x in out]:
            out.append(a)
    return out[:20]


async def create_project(
    uow: UnitOfWork,
    *,
    name: str,
    description: str | None,
    aliases: Sequence[str] | None,
    request_key: str,
    at: datetime.datetime,
) -> ProjectView:
    clean = " ".join(name.split())
    if not clean:
        raise ValidationFailed("name is required")
    project_id = uuid7()
    await uow.session.execute(
        insert(projects_table).values(
            id=project_id,
            user_id=uow.user_id,
            name=clean,
            description=description,
            aliases=_clean_aliases(aliases),
            status="active",
            origin="user",
            verification_status="user_created",
            user_fields=["name"],
            version=1,
        )
    )
    await _user_event(uow, project_id, "created", {"set": {"name": clean}}, request_key=request_key, at=at)
    await record_feedback(uow, target_type="project", target_id=project_id, action="create")
    return await get_project(uow, project_id)


async def edit_project(
    uow: UnitOfWork,
    project_id: UUID,
    changes: dict[str, Any],
    *,
    if_match: int | None,
    request_key: str,
    at: datetime.datetime,
) -> ProjectView:
    unknown = set(changes) - EDITABLE
    if unknown:
        raise ValidationFailed(f"not editable: {sorted(unknown)}")
    current = await get_project(uow, project_id, for_update=True)
    if if_match is not None and if_match != current.version:
        raise PreconditionFailed("the project changed", details={"current_version": current.version})
    values: dict[str, Any] = {}
    if "name" in changes:
        values["name"] = " ".join(str(changes["name"] or "").split())
        if not values["name"]:
            raise ValidationFailed("name is required")
    if "description" in changes:
        values["description"] = changes["description"]
    if "aliases" in changes:
        values["aliases"] = _clean_aliases(changes["aliases"])
    if "importance_user" in changes:
        values["importance_user"] = changes["importance_user"]
    if "status" in changes:
        if changes["status"] not in ("active", "archived"):
            raise ValidationFailed("status must be active or archived")
        values["status"] = changes["status"]
    if not values:
        return current
    p = projects_table
    fields = sorted(set(current.user_fields) | set(values))
    await uow.session.execute(
        update(p)
        .where(p.c.id == project_id, p.c.user_id == uow.user_id)
        .values(**values, user_fields=fields, version=p.c.version + 1)
    )
    before = {k: getattr(current, k) for k in values}
    await _user_event(uow, project_id, "user_edit", {"set": sorted(values)}, request_key=request_key, at=at)
    await record_feedback(
        uow,
        target_type="project",
        target_id=project_id,
        action="edit",
        before={k: list(v) if isinstance(v, tuple) else v for k, v in before.items()},
        after=values,
    )
    return await get_project(uow, project_id)


async def verify_project(
    uow: UnitOfWork, project_id: UUID, *, confirm: bool, request_key: str, at: datetime.datetime
) -> ProjectView:
    """Confirm or reject a suggestion (authority 5). Confirming a confirmed project is a no-op."""
    current = await get_project(uow, project_id, for_update=True)
    target = "confirmed" if confirm else "rejected"
    if current.verification_status == target or (confirm and current.verification_status == "user_created"):
        return current
    if current.origin == "user" and not confirm:
        raise Conflict("a project you created cannot be rejected; archive or delete it instead")
    p = projects_table
    await uow.session.execute(
        update(p)
        .where(p.c.id == project_id, p.c.user_id == uow.user_id)
        .values(
            verification_status=target,
            user_fields=sorted(set(current.user_fields) | {"verification_status"}),
            version=p.c.version + 1,
        )
    )
    await _user_event(
        uow,
        project_id,
        "user_confirmed" if confirm else "user_rejected",
        {"set": {"verification_status": target}},
        request_key=request_key,
        at=at,
    )
    await record_feedback(
        uow,
        target_type="project",
        target_id=project_id,
        action="confirm" if confirm else "reject",
        before={"verification_status": current.verification_status},
        after={"verification_status": target},
    )
    return await get_project(uow, project_id)


async def assign_item(
    uow: UnitOfWork, project_id: UUID, item_id: UUID, *, request_key: str, at: datetime.datetime
) -> None:
    project = await get_project(uow, project_id)
    if project.verification_status == "rejected":
        raise Conflict("this project was rejected")
    await work.assign_project(uow, item_id, project_id, request_key=request_key, at=at)
    await record_feedback(
        uow,
        target_type="work_item",
        target_id=item_id,
        action="assign_project",
        after={"project_id": str(project_id)},
    )


@dataclass(frozen=True)
class Member:
    person_id: UUID
    role: str | None
    origin: str


async def members_of(uow: UnitOfWork, project_id: UUID) -> list[Member]:
    m = project_members_table
    rows = await uow.session.execute(
        select(m.c.person_id, m.c.role, m.c.origin)
        .where(m.c.user_id == uow.user_id, m.c.project_id == project_id)
        .order_by(m.c.origin.desc(), m.c.person_id)
    )
    return [Member(r.person_id, r.role, r.origin) for r in rows]


async def set_computed_members(uow: UnitOfWork, project_id: UUID, person_ids: Sequence[UUID]) -> None:
    """Replace the computed members; members the user added are kept."""
    m = project_members_table
    keep = list(person_ids)
    await uow.session.execute(
        m.delete().where(
            m.c.user_id == uow.user_id,
            m.c.project_id == project_id,
            m.c.origin == "computed",
            m.c.person_id.not_in(keep) if keep else literal(True),
        )
    )
    for person_id in keep:
        await uow.session.execute(
            insert(m)
            .values(project_id=project_id, person_id=person_id, user_id=uow.user_id, origin="computed")
            .on_conflict_do_nothing()
        )


async def existing_hint_keys(uow: UnitOfWork) -> tuple[set[str], set[str]]:
    """(hint keys of every project ever suggested, normalized names of live projects)."""
    p = projects_table
    keys = {
        r.hint_key
        for r in await uow.session.execute(
            select(p.c.hint_key).where(p.c.user_id == uow.user_id, p.c.hint_key.is_not(None))
        )
    }
    names = set()
    for r in await uow.session.execute(
        select(p.c.name, p.c.aliases).where(p.c.user_id == uow.user_id, p.c.deleted_at.is_(None))
    ):
        names.add(normalize_hint(r.name))
        names.update(normalize_hint(a) for a in (r.aliases or ()))
    return keys, names


async def insert_suggestion(uow: UnitOfWork, *, name: str, hint_key: str, sources: int) -> UUID | None:
    p = projects_table
    project_id = uuid7()
    inserted = (
        await uow.session.execute(
            insert(p)
            .values(
                id=project_id,
                user_id=uow.user_id,
                name=name,
                aliases=[],
                hint_key=hint_key,
                status="active",
                origin="ai",
                verification_status="suggested",
                suggestion_sources=sources,
                user_fields=[],
                version=1,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "hint_key"], index_where=p.c.hint_key.is_not(None)
            )
            .returning(p.c.id)
        )
    ).scalar_one_or_none()
    return None if inserted is None else UUID(str(inserted))


async def active_confirmed(uow: UnitOfWork) -> list[ProjectView]:
    p = projects_table
    rows = await uow.session.execute(
        _live(uow).where(p.c.verification_status.in_(CONFIRMED), p.c.status == "active").order_by(p.c.id)
    )
    return [_view(r) for r in rows]


def activity_status(last_activity_at: datetime.datetime | None, now: datetime.datetime) -> str:
    """Quiet after 30 days without linked activity (§11.8); computed at read time."""
    if last_activity_at is None or now - last_activity_at > QUIET_AFTER:
        return "quiet"
    return "active"
