"""Connector registry for the production worker (BACKEND_DESIGN.md §20).

Gmail (slice 1.5) and Google Calendar (slice 1.6) are registered when Google OAuth credentials
and the token key are configured: each connector gets an access-token provider that refreshes through
``connections`` in its own short transaction for the user (``needs_reauth`` on ``invalid_grant``).
Tests and evaluation runners build their own registry with the fake connectors.
"""

from __future__ import annotations

import httpx

from eca.connections import GoogleOAuthConfig, TokenCrypto, access_token
from eca.connectors import ConnectionInfo, ConnectorRegistry, GmailConnector, GoogleCalendarConnector
from eca.platform.config import Settings
from eca.platform.uow import UnitOfWorkFactory


def build_connector_registry(
    settings: Settings, uow_factory: UnitOfWorkFactory | None = None
) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    if (
        uow_factory is None
        or not settings.google_client_id
        or settings.google_client_secret is None
        or settings.token_kek is None
    ):
        return registry
    cfg = GoogleOAuthConfig(
        settings.google_client_id,
        settings.google_client_secret.get_secret_value(),
        settings.google_connect_redirect_uri,
    )
    crypto = TokenCrypto(settings.token_kek.get_secret_value(), version=settings.token_kek_version)
    http = httpx.AsyncClient()

    def gmail(info: ConnectionInfo) -> GmailConnector:
        async def token() -> str:
            async with uow_factory(user_id=info.user_id) as uow:
                return await access_token(
                    uow, cfg, crypto, http, connection_id=info.connection_id, capability="mail"
                )

        return GmailConnector(http, token, account_email=info.account_email)

    def calendar(info: ConnectionInfo) -> GoogleCalendarConnector:
        async def token() -> str:
            async with uow_factory(user_id=info.user_id) as uow:
                return await access_token(
                    uow, cfg, crypto, http, connection_id=info.connection_id, capability="calendar"
                )

        return GoogleCalendarConnector(http, token)

    registry.register_mail("google", gmail)
    registry.register_calendar("google", calendar)
    return registry
