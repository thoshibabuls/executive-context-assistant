"""Unit of work: one transaction per request or job, with the user context for RLS.

Every unit of work runs inside a single transaction. When a user is given, the transaction
first executes ``set_config('app.user_id', <uuid>, true)``, which is the parameterized form of
``SET LOCAL app.user_id = ...`` (BACKEND_DESIGN.md §17.1): the setting lives only for this
transaction, so it cannot leak to the next user of a pooled connection.

Without a user, ``app.user_id`` stays unset and every RLS-protected table returns no rows
(fail closed). Repositories never commit; this class commits on success and rolls back on any
exception.
"""

from __future__ import annotations

from types import TracebackType
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, async_sessionmaker

_SET_USER_SQL = text("SELECT set_config('app.user_id', :user_id, true)")


class UnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, user_id: UUID | None) -> None:
        if user_id is not None and not isinstance(user_id, UUID):
            raise TypeError("user_id must be a UUID")
        self._session_factory = session_factory
        self.user_id = user_id
        self._session: AsyncSession | None = None
        self._transaction: AsyncSessionTransaction | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork is not active")
        return self._session

    async def __aenter__(self) -> UnitOfWork:
        session = self._session_factory()
        try:
            transaction = await session.begin()
            if self.user_id is not None:
                await session.execute(_SET_USER_SQL, {"user_id": str(self.user_id)})
        except BaseException:
            await session.close()
            raise
        self._session = session
        self._transaction = transaction
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session, transaction = self._session, self._transaction
        self._session = None
        self._transaction = None
        if session is None or transaction is None:
            return
        try:
            if exc_type is None:
                await transaction.commit()
            else:
                await transaction.rollback()
        finally:
            await session.close()


class UnitOfWorkFactory:
    """Creates units of work bound to one session factory (stored on app state / worker context)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self, *, user_id: UUID | None) -> UnitOfWork:
        return UnitOfWork(self._session_factory, user_id=user_id)
