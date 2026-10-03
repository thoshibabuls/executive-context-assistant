"""Deletion in ``retrieval`` (BACKEND_DESIGN.md §13.3, §17.5): account deletion removes the user's
chunks and retrieval traces; the user's alias mentions go with ``people``."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, update

from eca.platform.uow import UnitOfWork
from eca.retrieval.models import chunks_table
from eca.retrieval.traces import purge_user_traces


async def purge_user(uow: UnitOfWork) -> None:
    await uow.session.execute(delete(chunks_table).where(chunks_table.c.user_id == uow.user_id))
    await purge_user_traces(uow)


async def detach_meetings(uow: UnitOfWork, meeting_ids: Sequence[UUID]) -> None:
    """Source purge of calendar meetings (Phase 4): transcript chunks of an uploaded recording
    stay (the upload belongs to no connection) and lose their meeting link."""
    ids = list(meeting_ids)
    if ids:
        t = chunks_table
        await uow.session.execute(
            update(t).where(t.c.user_id == uow.user_id, t.c.meeting_id.in_(ids)).values(meeting_id=None)
        )
