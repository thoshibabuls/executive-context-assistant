"""Deletion in ``connections`` (BACKEND_DESIGN.md §13.3): tokens revoked first, rows last."""

from __future__ import annotations

import datetime
from uuid import UUID

import httpx
import structlog
from sqlalchemy import delete, select, update

from eca.connections.models import connections_table, sync_cursors_table
from eca.connections.tokens import TokenCrypto
from eca.connectors import revoke_token
from eca.platform.uow import UnitOfWork

log = structlog.get_logger("eca.connections.purge")


async def revoke_all_tokens(
    uow: UnitOfWork, crypto: TokenCrypto | None, http: httpx.AsyncClient | None, *, now: datetime.datetime
) -> int:
    """Revoke every stored refresh token at the provider (best effort), then drop the ciphertext.

    A revocation that fails (network, already revoked) is logged without the token; the
    ciphertext is deleted either way, so no usable credential remains in the database.
    """
    t = connections_table
    rows = (
        await uow.session.execute(
            select(t.c.id, t.c.refresh_token_ciphertext).where(t.c.refresh_token_ciphertext.is_not(None))
        )
    ).all()
    for row in rows:
        if crypto is not None and http is not None:
            try:
                await revoke_token(
                    http, crypto.decrypt(bytes(row.refresh_token_ciphertext), connection_id=row.id)
                )
            except Exception as exc:
                log.warning("token_revoke_failed", connection_id=str(row.id), error_type=type(exc).__name__)
        await uow.session.execute(
            update(t)
            .where(t.c.id == row.id)
            .values(refresh_token_ciphertext=None, status="revoked", revoked_at=now)
        )
    return len(rows)


async def purge_connection(uow: UnitOfWork, connection_id: UUID) -> None:
    """After ingestion and communication detached their rows: cursors, then the connection."""
    await uow.session.execute(
        delete(sync_cursors_table).where(sync_cursors_table.c.connection_id == connection_id)
    )
    await uow.session.execute(delete(connections_table).where(connections_table.c.id == connection_id))


async def purge_user(uow: UnitOfWork) -> None:
    await uow.session.execute(delete(sync_cursors_table))
    await uow.session.execute(delete(connections_table))
