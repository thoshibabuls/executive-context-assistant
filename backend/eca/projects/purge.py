"""Deletion in ``projects`` (BACKEND_DESIGN.md §13.3, §17.5): members, then projects. Runs after
``work`` in account deletion, because work items reference projects."""

from __future__ import annotations

from sqlalchemy import delete

from eca.platform.uow import UnitOfWork
from eca.projects.models import project_members_table, projects_table


async def purge_user(uow: UnitOfWork) -> None:
    await uow.session.execute(
        delete(project_members_table).where(project_members_table.c.user_id == uow.user_id)
    )
    await uow.session.execute(delete(projects_table).where(projects_table.c.user_id == uow.user_id))
