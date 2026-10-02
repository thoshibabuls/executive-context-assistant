"""Connections routes (slice 1.2, BACKEND_DESIGN.md §16.5)."""

from __future__ import annotations

import datetime
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from eca.api.auth import CurrentUser, current_user, http_client, jwks, settings_dep, uow_factory
from eca.connections import (
    GoogleOAuthConfig,
    TokenCrypto,
    complete_connect,
    disconnect,
    list_connections,
    start_connect,
)
from eca.identity import JwksCache, get_profile
from eca.ingestion import MAIL_RESOURCE, SYNC_REQUESTED, SyncRequested
from eca.platform.audit import record_audit
from eca.platform.config import Settings
from eca.platform.errors import ValidationFailed
from eca.platform.events import NewEvent
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWorkFactory

router = APIRouter(prefix="/api/v1/connections")


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    capability: str


def _cfg(settings: Settings) -> GoogleOAuthConfig:
    if not settings.google_client_id or settings.google_client_secret is None:
        raise ValidationFailed("Google connect is not configured")
    return GoogleOAuthConfig(
        settings.google_client_id,
        settings.google_client_secret.get_secret_value(),
        settings.google_connect_redirect_uri,
    )


def _crypto(settings: Settings) -> TokenCrypto:
    if settings.token_kek is None:
        raise ValidationFailed("TOKEN_KEK is not configured")
    return TokenCrypto(settings.token_kek.get_secret_value(), version=settings.token_kek_version)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@router.get("")
async def list_(
    user: Annotated[CurrentUser, Depends(current_user)],
    factory: Annotated[UnitOfWorkFactory, Depends(uow_factory)],
) -> dict[str, object]:
    async with factory(user_id=user.user_id) as uow:
        rows = await list_connections(uow)
    return {
        "items": [
            {
                "id": str(c.id),
                "provider": c.provider,
                "account_email": c.account_email,
                "status": c.status,
                "capabilities": list(c.capabilities),
            }
            for c in rows
        ]
    }


@router.post("")
async def start(
    body: ConnectRequest,
    user: Annotated[CurrentUser, Depends(current_user)],
    factory: Annotated[UnitOfWorkFactory, Depends(uow_factory)],
    settings: Annotated[Settings, Depends(settings_dep)],
) -> dict[str, str]:
    if body.provider != "google":
        raise ValidationFailed(f"unsupported provider {body.provider!r}")
    async with factory(user_id=user.user_id) as uow:
        profile = await get_profile(uow)
        url = await start_connect(
            uow, _cfg(settings), capability=body.capability, now=_now(), login_hint=profile.email
        )
    return {"authorization_url": url}


@router.get("/google/callback")
async def callback(
    code: str,
    state: str,
    user: Annotated[CurrentUser, Depends(current_user)],
    factory: Annotated[UnitOfWorkFactory, Depends(uow_factory)],
    settings: Annotated[Settings, Depends(settings_dep)],
    http: Annotated[httpx.AsyncClient, Depends(http_client)],
    keys: Annotated[JwksCache, Depends(jwks)],
) -> RedirectResponse:
    async with factory(user_id=user.user_id) as uow:
        view = await complete_connect(
            uow, _cfg(settings), _crypto(settings), http, keys, state=state, code=code, now=_now()
        )
        await record_audit(
            uow,
            "connection_connected",
            target_type="connection",
            target_id=view.id,
            metadata={"capabilities": list(view.capabilities)},
        )
        if "mail" in view.capabilities:
            await publish(
                uow,
                NewEvent(
                    SYNC_REQUESTED,
                    "connection",
                    view.id,
                    SyncRequested(connection_id=view.id, resource=MAIL_RESOURCE, trigger="manual"),
                ),
            )
    return RedirectResponse(f"{settings.web_base_url}/settings/connections", status_code=302)


@router.post("/{connection_id}/sync", status_code=202)
async def sync(
    connection_id: UUID,
    user: Annotated[CurrentUser, Depends(current_user)],
    factory: Annotated[UnitOfWorkFactory, Depends(uow_factory)],
) -> dict[str, str]:
    async with factory(user_id=user.user_id) as uow:
        await publish(
            uow,
            NewEvent(
                SYNC_REQUESTED,
                "connection",
                connection_id,
                SyncRequested(connection_id=connection_id, resource=MAIL_RESOURCE, trigger="manual"),
            ),
        )
    return {"status": "queued"}


@router.delete("/{connection_id}", status_code=202)
async def remove(
    connection_id: UUID,
    user: Annotated[CurrentUser, Depends(current_user)],
    factory: Annotated[UnitOfWorkFactory, Depends(uow_factory)],
    settings: Annotated[Settings, Depends(settings_dep)],
    http: Annotated[httpx.AsyncClient, Depends(http_client)],
    purge: bool = False,
) -> dict[str, object]:
    async with factory(user_id=user.user_id) as uow:
        await disconnect(uow, _crypto(settings), http, connection_id=connection_id, now=_now())
        await record_audit(
            uow,
            "connection_disconnected",
            target_type="connection",
            target_id=connection_id,
            metadata={"purge_requested": purge},
        )
    return {"status": "revoked", "purge": purge, "purge_note": "source purge job arrives in slice 1.9"}
