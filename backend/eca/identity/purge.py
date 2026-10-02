"""Account deletion in ``identity`` (BACKEND_DESIGN.md §13.3): the last rows of a user.

The worker deletes the user row under ``users_worker_delete`` (own row, status ``deleting``).
"""

from __future__ import annotations

from sqlalchemy import delete

from eca.identity.models import users_table
from eca.identity.sessions import auth_sessions_table
from eca.platform.uow import UnitOfWork


async def purge_sessions(uow: UnitOfWork) -> None:
    await uow.session.execute(delete(auth_sessions_table))


async def delete_user_row(uow: UnitOfWork) -> None:
    assert uow.user_id is not None
    u = users_table
    await uow.session.execute(delete(u).where(u.c.id == uow.user_id, u.c.status == "deleting"))
