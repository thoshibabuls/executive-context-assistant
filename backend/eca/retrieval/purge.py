"""Deletion in ``retrieval`` (BACKEND_DESIGN.md §13.3, §17.5): account deletion removes the user's
chunks (and, from slice 2.2, retrieval traces); the user's alias mentions go with ``people``."""

from __future__ import annotations

from sqlalchemy import delete

from eca.platform.uow import UnitOfWork
from eca.retrieval.models import chunks_table


async def purge_user(uow: UnitOfWork) -> None:
    await uow.session.execute(delete(chunks_table).where(chunks_table.c.user_id == uow.user_id))
