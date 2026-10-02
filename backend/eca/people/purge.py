"""Deletion in ``people`` (BACKEND_DESIGN.md §13.3). Children before parents, no cascades."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, update

from eca.people.models import (
    entity_mentions_table,
    organizations_table,
    person_identifiers_table,
    persons_table,
)
from eca.platform.uow import UnitOfWork


async def purge_mentions_for_sources(uow: UnitOfWork, source_ids: Sequence[UUID]) -> None:
    """Mentions point at source items and evidence; they go before either."""
    em = entity_mentions_table
    await uow.session.execute(delete(em).where(em.c.source_item_id.in_(list(source_ids))))


async def purge_mentions(uow: UnitOfWork) -> None:
    await uow.session.execute(delete(entity_mentions_table))


async def purge_user(uow: UnitOfWork) -> None:
    p = persons_table
    await uow.session.execute(delete(entity_mentions_table))
    await uow.session.execute(delete(person_identifiers_table))
    await uow.session.execute(update(p).values(merged_into_id=None, organization_id=None))
    await uow.session.execute(delete(p))
    await uow.session.execute(delete(organizations_table))
