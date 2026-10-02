"""Person and organization resolution (TECHNICAL_DESIGN.md §13.7, CONTEXT_ARCHITECTURE.md §11.1).

- Normalized email is the identity; display names become aliases; persons merge only by a user
  action (``merge_persons``), never automatically. Two addresses of one human stay two persons
  until the user merges them (CC-41); similar names never merge (CC-42).
- Name-only mentions resolve only within the given participants, else stay unresolved.
- Organizations come from non-public email domains.

Person creation is serialized per user with a transaction-level advisory lock, so two
concurrent normalizations of the same new address create one person.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from eca.people.identity_rules import (
    email_domain,
    first_name,
    is_public_domain,
    normalize_alias,
    normalize_email,
    organization_name,
)
from eca.people.models import (
    entity_mentions_table,
    organizations_table,
    person_identifiers_table,
    persons_table,
)
from eca.platform.errors import Conflict, NotFound, ValidationFailed
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork

_PEOPLE_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended('people:' || :user_id, 0))")


@dataclass(frozen=True)
class PersonRef:
    id: UUID
    display_name: str | None
    primary_email: str | None
    is_self: bool
    organization_id: UUID | None


@dataclass(frozen=True)
class MentionIn:
    source_item_id: UUID
    entity_type: str
    entity_id: UUID
    surface_text: str
    confidence: float
    method: str
    occurred_at: datetime.datetime
    evidence_id: UUID | None = None
    chunk_id: UUID | None = None


def _ref(row: object) -> PersonRef:
    return PersonRef(
        id=row.id,  # type: ignore[attr-defined]
        display_name=row.display_name,  # type: ignore[attr-defined]
        primary_email=row.primary_email,  # type: ignore[attr-defined]
        is_self=row.is_self,  # type: ignore[attr-defined]
        organization_id=row.organization_id,  # type: ignore[attr-defined]
    )


_PERSON_COLS = (
    persons_table.c.id,
    persons_table.c.display_name,
    persons_table.c.primary_email,
    persons_table.c.is_self,
    persons_table.c.organization_id,
    persons_table.c.merged_into_id,
)


async def _lock(uow: UnitOfWork) -> None:
    await uow.session.execute(_PEOPLE_LOCK_SQL, {"user_id": str(uow.user_id)})


async def _person(uow: UnitOfWork, person_id: UUID) -> PersonRef:
    """Load a person, following one ``merged_into_id`` redirect (BACKEND_DESIGN.md §12.5)."""
    row = (
        await uow.session.execute(select(*_PERSON_COLS).where(persons_table.c.id == person_id))
    ).one_or_none()
    if row is None:
        raise NotFound(f"person {person_id} not found")
    if row.merged_into_id is not None:
        row = (
            await uow.session.execute(select(*_PERSON_COLS).where(persons_table.c.id == row.merged_into_id))
        ).one()
    return _ref(row)


async def _organization_for(uow: UnitOfWork, domain: str) -> UUID | None:
    if is_public_domain(domain):
        return None
    existing = (
        await uow.session.execute(
            select(organizations_table.c.id).where(
                organizations_table.c.user_id == uow.user_id, organizations_table.c.domain == domain
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return UUID(str(existing))
    org_id = uuid7()
    await uow.session.execute(
        insert(organizations_table).values(
            id=org_id, user_id=uow.user_id, name=organization_name(domain), domain=domain, origin="computed"
        )
    )
    return org_id


async def _add_identifier(
    uow: UnitOfWork, person_id: UUID, kind: str, value: str, source: str, *, confirmed: bool = False
) -> None:
    stmt = insert(person_identifiers_table).values(
        id=uuid7(),
        user_id=uow.user_id,
        person_id=person_id,
        kind=kind,
        value_normalized=value,
        source=source,
        confidence=1.0,
        confirmed=confirmed,
    )
    await uow.session.execute(stmt.on_conflict_do_nothing())


async def create_self_person(uow: UnitOfWork, *, email: str, display_name: str) -> PersonRef:
    """The user's own Person (``is_self``), created with the user. Idempotent."""
    if uow.user_id is None:
        raise ValidationFailed("create_self_person needs a user unit of work")
    await _lock(uow)
    existing = (
        await uow.session.execute(
            select(*_PERSON_COLS).where(persons_table.c.user_id == uow.user_id, persons_table.c.is_self)
        )
    ).one_or_none()
    if existing is not None:
        return _ref(existing)
    normalized = normalize_email(email)
    org_id = await _organization_for(uow, email_domain(normalized))
    person_id = uuid7()
    await uow.session.execute(
        insert(persons_table).values(
            id=person_id,
            user_id=uow.user_id,
            display_name=display_name,
            primary_email=normalized,
            organization_id=org_id,
            is_self=True,
            version=1,
        )
    )
    await _add_identifier(uow, person_id, "email", normalized, "self", confirmed=True)
    await _add_identifier(uow, person_id, "name_alias", normalize_alias(display_name), "self", confirmed=True)
    return PersonRef(person_id, display_name, normalized, True, org_id)


async def get_self_person(uow: UnitOfWork) -> PersonRef:
    row = (
        await uow.session.execute(
            select(*_PERSON_COLS).where(persons_table.c.user_id == uow.user_id, persons_table.c.is_self)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("the user has no self Person")
    return _ref(row)


async def find_by_email(uow: UnitOfWork, email: str) -> PersonRef | None:
    try:
        normalized = normalize_email(email)
    except ValueError:
        return None
    person_id = (
        await uow.session.execute(
            select(person_identifiers_table.c.person_id).where(
                person_identifiers_table.c.user_id == uow.user_id,
                person_identifiers_table.c.kind == "email",
                person_identifiers_table.c.value_normalized == normalized,
            )
        )
    ).scalar_one_or_none()
    return None if person_id is None else await _person(uow, person_id)


_NAME_KEY_SQL = text(
    """
    UPDATE persons
       SET display_name = :name,
           interaction_stats = jsonb_set(interaction_stats, '{name_key}', to_jsonb(CAST(:key AS text)))
     WHERE id = :id AND NOT is_self
       AND (interaction_stats->>'name_key' IS NULL OR interaction_stats->>'name_key' < :key)
    """
)


async def resolve_address(
    uow: UnitOfWork, *, email: str, display_name: str | None, seen_at: datetime.datetime | None = None
) -> PersonRef:
    """Find or create the person for an address; record the display name as an alias.

    The person's display name follows the newest header that carried one (ties broken by the
    name), so the result does not depend on the order messages are processed (RT-04).
    """
    normalized = normalize_email(email)
    found = await find_by_email(uow, normalized)
    if found is None:
        await _lock(uow)
        found = await find_by_email(uow, normalized)  # re-check under the lock
    if found is None:
        org_id = await _organization_for(uow, email_domain(normalized))
        person_id = uuid7()
        await uow.session.execute(
            insert(persons_table).values(
                id=person_id,
                user_id=uow.user_id,
                display_name=display_name or None,
                primary_email=normalized,
                organization_id=org_id,
                is_self=False,
                version=1,
            )
        )
        await _add_identifier(uow, person_id, "email", normalized, "header")
        found = PersonRef(person_id, display_name or None, normalized, False, org_id)
    if display_name:
        alias = normalize_alias(display_name)
        if alias and "@" not in alias:
            await _add_identifier(uow, found.id, "name_alias", alias, "header")
            if seen_at is not None:
                key = f"{seen_at.astimezone(datetime.UTC).isoformat()}|{display_name.strip()}"
                await uow.session.execute(
                    _NAME_KEY_SQL, {"name": display_name.strip(), "key": key, "id": found.id}
                )
    return found


async def record_interaction(
    uow: UnitOfWork, person_id: UUID, *, at: datetime.datetime, inbound: bool | None
) -> None:
    """Interaction timestamps (computed; order-independent: max of what was seen)."""
    p = persons_table.c
    values: dict[str, object] = {
        "first_seen_at": func.least(func.coalesce(p.first_seen_at, at), at),
        "last_interaction_at": func.greatest(func.coalesce(p.last_interaction_at, at), at),
    }
    if inbound is True:
        values["last_inbound_at"] = func.greatest(func.coalesce(p.last_inbound_at, at), at)
    elif inbound is False:
        values["last_outbound_at"] = func.greatest(func.coalesce(p.last_outbound_at, at), at)
    await uow.session.execute(update(persons_table).where(p.id == person_id).values(**values))


async def get_persons(uow: UnitOfWork, ids: Iterable[UUID]) -> dict[UUID, PersonRef]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = await uow.session.execute(select(*_PERSON_COLS).where(persons_table.c.id.in_(wanted)))
    out: dict[UUID, PersonRef] = {}
    for row in rows:
        out[row.id] = await _person(uow, row.id) if row.merged_into_id is not None else _ref(row)
    return out


async def aliases_of(uow: UnitOfWork, person_ids: Sequence[UUID]) -> dict[UUID, list[str]]:
    rows = await uow.session.execute(
        select(person_identifiers_table.c.person_id, person_identifiers_table.c.value_normalized).where(
            person_identifiers_table.c.person_id.in_(list(person_ids)),
            person_identifiers_table.c.kind == "name_alias",
        )
    )
    out: dict[UUID, list[str]] = {pid: [] for pid in person_ids}
    for row in rows:
        out[row.person_id].append(row.value_normalized)
    return out


async def resolve_name(uow: UnitOfWork, surface: str, participants: Sequence[PersonRef]) -> PersonRef | None:
    """Resolve a name mention within the message's participants only (§13.7).

    Full-alias match first, then a unique first-name match; anything ambiguous stays unresolved.
    """
    alias = normalize_alias(surface)
    if not alias or not participants:
        return None
    by_person = await aliases_of(uow, [p.id for p in participants])
    full = [p for p in participants if alias in by_person.get(p.id, [])]
    if len(full) == 1:
        return full[0]
    first = first_name(alias)
    partial = [p for p in participants if any(first_name(a) == first for a in by_person.get(p.id, []))]
    return partial[0] if len(partial) == 1 else None


async def record_mentions(uow: UnitOfWork, mentions: Sequence[MentionIn]) -> None:
    for m in mentions:
        await uow.session.execute(
            insert(entity_mentions_table)
            .values(
                id=uuid7(),
                user_id=uow.user_id,
                source_item_id=m.source_item_id,
                evidence_id=m.evidence_id,
                chunk_id=m.chunk_id,
                entity_type=m.entity_type,
                entity_id=m.entity_id,
                surface_text=m.surface_text,
                confidence=m.confidence,
                method=m.method,
                occurred_at=m.occurred_at,
            )
            .on_conflict_do_nothing()
        )


async def merge_persons(uow: UnitOfWork, *, source_id: UUID, target_id: UUID) -> None:
    """User action: merge ``source`` into ``target`` (identifiers move; source redirects)."""
    if source_id == target_id:
        raise ValidationFailed("cannot merge a person into itself")
    await _lock(uow)
    for pid in sorted((source_id, target_id)):  # lock order: persons by ascending id (§12.2)
        await uow.session.execute(
            select(persons_table.c.id).where(persons_table.c.id == pid).with_for_update()
        )
    source, target = await _person(uow, source_id), await _person(uow, target_id)
    if source.is_self or target.id == source.id:
        raise Conflict("this merge is not allowed")
    ids = person_identifiers_table.c
    rows = await uow.session.execute(
        select(ids.kind, ids.value_normalized, ids.source).where(ids.person_id == source.id)
    )
    for row in rows.all():
        await _add_identifier(uow, target.id, row.kind, row.value_normalized, "user", confirmed=True)
    await uow.session.execute(
        update(person_identifiers_table).where(ids.person_id == source.id).values(confirmed=True)
    )
    await uow.session.execute(
        update(persons_table)
        .where(persons_table.c.id == source.id)
        .values(merged_into_id=target.id, version=persons_table.c.version + 1)
    )


async def merged_ids(uow: UnitOfWork, person_id: UUID) -> list[UUID]:
    """The person and every person merged into it (for "What did Alex promise?", CC-41)."""
    rows = await uow.session.execute(
        select(persons_table.c.id).where(
            (persons_table.c.id == person_id) | (persons_table.c.merged_into_id == person_id)
        )
    )
    return sorted(r.id for r in rows)


@dataclass(frozen=True)
class AliasEntry:
    person_id: UUID
    alias: str  # normalized (lower case, single spaces)


ALIAS_MIN_WORDS = 2
ALIAS_MIN_CHARS = 5


async def alias_catalog(uow: UnitOfWork) -> list[AliasEntry]:
    """Name aliases usable for the alias scan (CONTEXT_ARCHITECTURE.md §12.7): at least two words
    and five characters, of persons that are not the user and not merged into another person."""
    ids, p = person_identifiers_table, persons_table
    rows = await uow.session.execute(
        select(ids.c.person_id, ids.c.value_normalized)
        .join(p, p.c.id == ids.c.person_id)
        .where(
            ids.c.user_id == uow.user_id,
            ids.c.kind == "name_alias",
            ~p.c.is_self,
            p.c.merged_into_id.is_(None),
            p.c.deleted_at.is_(None),
            func.length(ids.c.value_normalized) >= ALIAS_MIN_CHARS,
        )
        .order_by(ids.c.value_normalized, ids.c.person_id)
    )
    return [
        AliasEntry(r.person_id, r.value_normalized)
        for r in rows
        if len(r.value_normalized.split(" ")) >= ALIAS_MIN_WORDS
    ]


async def replace_alias_mentions(
    uow: UnitOfWork, source_item_id: UUID, mentions: Sequence[MentionIn]
) -> None:
    """Re-index of one source: its ``alias_match`` mentions are replaced (other methods are kept)."""
    em = entity_mentions_table
    await uow.session.execute(
        delete(em).where(
            em.c.user_id == uow.user_id, em.c.source_item_id == source_item_id, em.c.method == "alias_match"
        )
    )
    for m in mentions:
        if m.method != "alias_match" or m.source_item_id != source_item_id:
            raise ValidationFailed("replace_alias_mentions takes alias_match mentions of one source")
        await uow.session.execute(
            insert(entity_mentions_table)
            .values(
                id=uuid7(),
                user_id=uow.user_id,
                source_item_id=m.source_item_id,
                chunk_id=m.chunk_id,
                entity_type=m.entity_type,
                entity_id=m.entity_id,
                surface_text=m.surface_text,
                confidence=m.confidence,
                method=m.method,
                occurred_at=m.occurred_at,
            )
            .on_conflict_do_nothing()
        )


@dataclass(frozen=True)
class MentionCount:
    source_item_id: UUID
    occurred_at: datetime.datetime


async def mentions_of(
    uow: UnitOfWork, entity_type: str, entity_ids: Sequence[UUID], *, since: datetime.datetime
) -> list[MentionCount]:
    """Sources mentioning the entities since ``since``, newest first (person and project context)."""
    ids = list(entity_ids)
    if not ids:
        return []
    em = entity_mentions_table
    rows = await uow.session.execute(
        select(em.c.source_item_id, func.max(em.c.occurred_at).label("at"))
        .where(
            em.c.user_id == uow.user_id,
            em.c.entity_type == entity_type,
            em.c.entity_id.in_(ids),
            em.c.occurred_at >= since,
        )
        .group_by(em.c.source_item_id)
        .order_by(func.max(em.c.occurred_at).desc(), em.c.source_item_id)
    )
    return [MentionCount(r.source_item_id, r.at) for r in rows]
