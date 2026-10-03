"""People and organization read models and edits for the API (slice 1.7, BACKEND_DESIGN.md §16.5).

User edits (importance, role, relationship type, name, organization name) are USER-AUTHORED:
recorded in ``persons.user_fields`` (authority 5), as feedback, and announced with
``PersonChanged``; merges and aliases go through the identity rules in ``service``. The computed
relationship profile (CONTEXT_ARCHITECTURE.md §5.3) is read here, never written.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Numeric, and_, func, or_, select, update

from eca.people.identity_rules import normalize_alias, normalize_email
from eca.people.models import organizations_table, person_identifiers_table, persons_table
from eca.people.service import _add_identifier, _lock, publish_person_changed
from eca.platform.errors import NotFound, PreconditionFailed, ValidationFailed
from eca.platform.feedback import record_feedback
from eca.platform.uow import UnitOfWork

RELATIONSHIP_TYPES = frozenset(
    {
        "executive",
        "client",
        "investor",
        "manager",
        "report",
        "partner",
        "stakeholder",
        "colleague",
        "vendor",
        "other",
        "low_priority",
    }
)


@dataclass(frozen=True)
class PersonSummary:
    id: UUID
    display_name: str | None
    primary_email: str | None
    organization_id: UUID | None
    role_title: str | None
    importance_user: int | None
    is_self: bool
    last_interaction_at: datetime.datetime | None
    version: int
    relationship_type: str | None = None
    role_origin: str | None = None
    importance_inferred: float | None = None
    user_fields: tuple[str, ...] = ()
    profile: dict[str, Any] | None = None  # computed (CONTEXT_ARCHITECTURE.md §5.3)
    profile_computed_at: datetime.datetime | None = None


@dataclass(frozen=True)
class PersonDetail:
    person: PersonSummary
    identifiers: list[dict[str, Any]]
    organization: dict[str, Any] | None
    interaction_stats: dict[str, Any]
    last_inbound_at: datetime.datetime | None
    last_outbound_at: datetime.datetime | None


def _summary(r: Any) -> PersonSummary:
    stats = dict(r.interaction_stats or {})
    profile = stats.get("profile")
    return PersonSummary(
        r.id,
        r.display_name,
        r.primary_email,
        r.organization_id,
        r.role_title,
        r.importance_user,
        r.is_self,
        r.last_interaction_at,
        r.version,
        relationship_type=r.relationship_type,
        role_origin=r.role_origin,
        importance_inferred=r.importance_inferred,
        user_fields=tuple(r.user_fields or ()),
        profile=dict(profile) if isinstance(profile, dict) else None,
        profile_computed_at=r.profile_computed_at,
    )


async def list_people_page(
    uow: UnitOfWork, *, q: str | None, sort: str, after: list[Any] | None, limit: int
) -> tuple[list[PersonSummary], list[Any] | None]:
    """``sort=importance`` (user importance, then inferred importance, then recent interaction) or
    ``sort=recent``."""
    p = persons_table
    stmt = select(p).where(p.c.merged_into_id.is_(None), p.c.deleted_at.is_(None), ~p.c.is_self)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(func.lower(p.c.display_name).like(like), func.lower(p.c.primary_email).like(like))
        )
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
    seen = func.coalesce(p.c.last_interaction_at, epoch)
    imp = func.coalesce(p.c.importance_user, 0)
    inferred = func.round(func.cast(func.coalesce(p.c.importance_inferred, 0), Numeric(6, 3)), 3)
    if sort == "importance":
        order = [imp.desc(), inferred.desc(), seen.desc(), p.c.id]
        if after is not None:
            i0, f0 = int(after[0]), Decimal(str(after[1]))
            t0, id0 = datetime.datetime.fromisoformat(after[2]), UUID(after[3])
            stmt = stmt.where(
                or_(
                    imp < i0,
                    and_(imp == i0, inferred < f0),
                    and_(imp == i0, inferred == f0, seen < t0),
                    and_(imp == i0, inferred == f0, seen == t0, p.c.id > id0),
                )
            )
    elif sort == "recent":
        order = [seen.desc(), p.c.id]
        if after is not None:
            t0, id0 = datetime.datetime.fromisoformat(after[2]), UUID(after[3])
            stmt = stmt.where(or_(seen < t0, and_(seen == t0, p.c.id > id0)))
    else:
        raise ValidationFailed("sort must be importance or recent")
    stmt = stmt.add_columns(inferred.label("inferred_key")).order_by(*order).limit(limit + 1)
    rows = (await uow.session.execute(stmt)).all()
    page = rows[:limit]
    next_key = None
    if len(rows) > limit and page:
        last = page[-1]
        next_key = [
            last.importance_user or 0,
            str(last.inferred_key),
            (last.last_interaction_at or epoch).isoformat(),
            str(last.id),
        ]
    return [_summary(r) for r in page], next_key


async def person_detail(uow: UnitOfWork, person_id: UUID) -> PersonDetail:
    p, ids, o = persons_table, person_identifiers_table, organizations_table
    row = (
        await uow.session.execute(select(p).where(p.c.id == person_id, p.c.deleted_at.is_(None)))
    ).one_or_none()
    if row is None:
        raise NotFound("person not found")
    if row.merged_into_id is not None:
        return await person_detail(uow, row.merged_into_id)
    identifiers = [
        {"kind": r.kind, "value": r.value_normalized, "source": r.source, "confirmed": r.confirmed}
        for r in await uow.session.execute(
            select(ids.c.kind, ids.c.value_normalized, ids.c.source, ids.c.confirmed)
            .where(ids.c.person_id == person_id)
            .order_by(ids.c.kind, ids.c.value_normalized)
        )
    ]
    org = None
    if row.organization_id is not None:
        o_row = (
            await uow.session.execute(
                select(o.c.id, o.c.name, o.c.domain).where(o.c.id == row.organization_id)
            )
        ).one_or_none()
        if o_row is not None:
            org = {"id": str(o_row.id), "name": o_row.name, "domain": o_row.domain}
    return PersonDetail(
        _summary(row),
        identifiers,
        org,
        dict(row.interaction_stats or {}),
        row.last_inbound_at,
        row.last_outbound_at,
    )


PERSON_EDITABLE = frozenset({"importance_user", "role_title", "display_name", "relationship_type"})


async def edit_person(
    uow: UnitOfWork, person_id: UUID, changes: dict[str, Any], *, if_match: int | None
) -> PersonSummary:
    unknown = set(changes) - PERSON_EDITABLE
    if unknown or not changes:
        raise ValidationFailed(f"not editable: {sorted(unknown)}" if unknown else "no fields to change")
    imp = changes.get("importance_user")
    if imp is not None and imp not in (1, 2, 3, 4, 5):
        raise ValidationFailed("importance_user must be 1-5 or null")
    kind = changes.get("relationship_type")
    if kind is not None and kind not in RELATIONSHIP_TYPES:
        raise ValidationFailed(f"relationship_type must be one of {sorted(RELATIONSHIP_TYPES)} or null")
    if "display_name" in changes and not (changes["display_name"] or "").strip():
        raise ValidationFailed("display_name must not be blank")
    p = persons_table
    cur = (
        await uow.session.execute(
            select(p).where(p.c.id == person_id, p.c.merged_into_id.is_(None)).with_for_update()
        )
    ).one_or_none()
    if cur is None:
        raise NotFound("person not found")
    if if_match is not None and if_match != cur.version:
        raise PreconditionFailed("the person changed", details={"current_version": cur.version})
    values = dict(changes)
    if "role_title" in values:
        values["role_origin"] = "user" if values["role_title"] else None
    # A field set to a value is user-authored (authority 5); clearing it hands it back to computation.
    user_fields = set(cur.user_fields or ()) | {k for k, v in changes.items() if v is not None}
    user_fields -= {k for k, v in changes.items() if v is None}
    values["user_fields"] = sorted(user_fields)
    new = (
        await uow.session.execute(
            update(p).where(p.c.id == person_id).values(**values, version=p.c.version + 1).returning(*p.c)
        )
    ).one()
    await record_feedback(
        uow,
        target_type="person",
        target_id=person_id,
        action="edit",
        before={k: getattr(cur, k) for k in changes},
        after=changes,
    )
    await publish_person_changed(uow, person_id, "edit", fields=list(changes))
    return _summary(new)


async def add_alias(uow: UnitOfWork, person_id: UUID, *, kind: str, value: str) -> None:
    """User-confirmed identifier: ``email`` or ``name`` alias."""
    if kind not in ("email", "name"):
        raise ValidationFailed("kind must be email or name")
    normalized = normalize_email(value) if kind == "email" else normalize_alias(value)
    if not normalized:
        raise ValidationFailed("empty alias")
    await _lock(uow)
    p = persons_table
    exists_ = (
        await uow.session.execute(select(p.c.id).where(p.c.id == person_id, p.c.merged_into_id.is_(None)))
    ).one_or_none()
    if exists_ is None:
        raise NotFound("person not found")
    await _add_identifier(uow, person_id, kind, normalized, "user", confirmed=True)
    await record_feedback(
        uow, target_type="person", target_id=person_id, action="add_alias", after={"kind": kind}
    )
    await publish_person_changed(uow, person_id, "alias")


@dataclass(frozen=True)
class OrganizationView:
    id: UUID
    name: str
    domain: str | None
    importance_user: int | None
    origin: str


async def list_organizations(uow: UnitOfWork) -> list[OrganizationView]:
    o = organizations_table
    rows = await uow.session.execute(
        select(o.c.id, o.c.name, o.c.domain, o.c.importance_user, o.c.origin)
        .where(o.c.deleted_at.is_(None))
        .order_by(o.c.name, o.c.id)
    )
    return [OrganizationView(r.id, r.name, r.domain, r.importance_user, r.origin) for r in rows]


async def edit_organization(uow: UnitOfWork, org_id: UUID, changes: dict[str, Any]) -> OrganizationView:
    unknown = set(changes) - {"name", "importance_user"}
    if unknown or not changes:
        raise ValidationFailed(f"not editable: {sorted(unknown)}" if unknown else "no fields to change")
    o = organizations_table
    row = (
        await uow.session.execute(
            update(o)
            .where(o.c.id == org_id, o.c.deleted_at.is_(None))
            .values(**changes)
            .returning(o.c.id, o.c.name, o.c.domain, o.c.importance_user, o.c.origin)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("organization not found")
    await record_feedback(uow, target_type="organization", target_id=org_id, action="edit", after=changes)
    return OrganizationView(row.id, row.name, row.domain, row.importance_user, row.origin)


async def importance_of(uow: UnitOfWork, person_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    """Inputs of the priority sender and organization features (TECHNICAL_DESIGN.md §12.6)."""
    if not person_ids:
        return {}
    p, o = persons_table, organizations_table
    rows = await uow.session.execute(
        select(
            p.c.id,
            p.c.importance_user,
            p.c.importance_inferred,
            p.c.interaction_stats,
            p.c.last_outbound_at,
            p.c.is_self,
            o.c.importance_user.label("org_importance"),
        )
        .outerjoin(o, o.c.id == p.c.organization_id)
        .where(p.c.id.in_(person_ids))
    )
    return {
        r.id: {
            "importance_user": r.importance_user,
            "importance_inferred": r.importance_inferred,
            "org_importance": r.org_importance,
            "stats": dict(r.interaction_stats or {}),
            "known": r.last_outbound_at is not None or r.importance_user is not None,
            "is_self": r.is_self,
        }
        for r in rows
    }


@dataclass(frozen=True)
class ProfileSubject:
    """A person whose relationship profile is computed (CONTEXT_ARCHITECTURE.md §5.3)."""

    id: UUID
    role_title: str | None
    org_importance: int | None
    last_interaction_at: datetime.datetime | None
    last_inbound_at: datetime.datetime | None
    last_outbound_at: datetime.datetime | None
    merged_ids: tuple[UUID, ...]  # the person and everyone merged into it


PROFILE_ACTIVE_DAYS = 180
PROFILE_MAX_PERSONS = 2000


async def profile_subjects(
    uow: UnitOfWork, *, now: datetime.datetime, person_ids: list[UUID] | None = None
) -> list[ProfileSubject]:
    """Live, non-self persons to profile: the given ones (merged IDs redirected), else everyone
    with an interaction in the last 180 days or an inferred importance still decaying."""
    p, o = persons_table, organizations_table
    stmt = (
        select(
            p.c.id,
            p.c.role_title,
            p.c.last_interaction_at,
            p.c.last_inbound_at,
            p.c.last_outbound_at,
            o.c.importance_user.label("org_importance"),
        )
        .outerjoin(o, o.c.id == p.c.organization_id)
        .where(
            p.c.user_id == uow.user_id,
            ~p.c.is_self,
            p.c.merged_into_id.is_(None),
            p.c.deleted_at.is_(None),
        )
    )
    if person_ids is not None:
        if not person_ids:
            return []
        targets = select(func.coalesce(p.c.merged_into_id, p.c.id)).where(p.c.id.in_(person_ids))
        stmt = stmt.where(p.c.id.in_(targets))
    else:
        since = now - datetime.timedelta(days=PROFILE_ACTIVE_DAYS)
        stmt = stmt.where(or_(p.c.last_interaction_at >= since, p.c.importance_inferred > 0))
    rows = (await uow.session.execute(stmt.order_by(p.c.id).limit(PROFILE_MAX_PERSONS))).all()
    if not rows:
        return []
    merged: dict[UUID, list[UUID]] = {r.id: [r.id] for r in rows}
    for m in await uow.session.execute(
        select(p.c.id, p.c.merged_into_id).where(
            p.c.user_id == uow.user_id, p.c.merged_into_id.in_(list(merged))
        )
    ):
        merged[m.merged_into_id].append(m.id)
    return [
        ProfileSubject(
            r.id,
            r.role_title,
            r.org_importance,
            r.last_interaction_at,
            r.last_inbound_at,
            r.last_outbound_at,
            tuple(sorted(merged[r.id])),
        )
        for r in rows
    ]
