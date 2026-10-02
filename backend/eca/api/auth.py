"""Authentication and session dependencies; auth and ``/me`` routes (slice 1.1).

Composition code (``eca.api``): it wires ``identity`` (OIDC, sessions, CSRF), ``people`` (self
Person on first sign-in) and ``platform.audit``. Cookies: session token ``HttpOnly``, ``Secure``
(configurable for local HTTP), ``SameSite=Lax``; CSRF token readable by the web app and echoed in
``X-CSRF-Token`` on unsafe methods. There is no other way to authenticate (no bypass).
"""

from __future__ import annotations

import datetime
import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from eca.identity import (
    JwksCache,
    authorization_url,
    check_csrf,
    consume_state,
    create_session,
    create_state,
    create_user,
    discard_user_states,
    exchange_code,
    find_signin_user,
    get_profile,
    link_google_identity,
    load_session,
    request_account_deletion,
    revoke_all_sessions,
    revoke_session,
    session_user_id,
    verify_id_token,
)
from eca.people import create_self_person
from eca.platform.audit import record_audit
from eca.platform.config import Settings, get_settings
from eca.platform.errors import Gone, PermissionDenied, ValidationFailed
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWorkFactory

router = APIRouter(prefix="/api/v1")
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
STATE_COOKIE = "eca_oauth_state"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def uow_factory(request: Request) -> UnitOfWorkFactory:
    factory = request.app.state.uow_factory
    if not isinstance(factory, UnitOfWorkFactory):
        raise RuntimeError("Database is not configured")
    return factory


def http_client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.http
    return client


def jwks(request: Request) -> JwksCache:
    cache: JwksCache = request.app.state.jwks
    return cache


def settings_dep() -> Settings:
    return get_settings()


@dataclass(frozen=True)
class CurrentUser:
    user_id: UUID
    session_id: UUID
    reauth_at: datetime.datetime


async def current_user(
    request: Request,
    factory: Annotated[UnitOfWorkFactory, Depends(uow_factory)],
    settings: Annotated[Settings, Depends(settings_dep)],
) -> CurrentUser:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise PermissionDenied("not signed in")
    async with factory(user_id=None) as uow:
        user_id = await session_user_id(uow, token)
    if user_id is None:
        raise PermissionDenied("session expired or revoked")
    async with factory(user_id=user_id) as uow:
        info = await load_session(uow, token, now=_now())
    if info is None:
        raise PermissionDenied("session expired or revoked")
    if info.user_status != "active":
        raise Gone("this account is being deleted")
    if request.method in UNSAFE:
        check_csrf(info, request.headers.get("x-csrf-token"))
    return CurrentUser(user_id=info.user_id, session_id=info.session_id, reauth_at=info.reauth_at)


def _require_google(settings: Settings) -> tuple[str, str]:
    if not settings.google_client_id or settings.google_client_secret is None:
        raise ValidationFailed("Google sign-in is not configured")
    return settings.google_client_id, settings.google_client_secret.get_secret_value()


@router.get("/auth/google/login")
async def login(
    factory: Annotated[UnitOfWorkFactory, Depends(uow_factory)],
    settings: Annotated[Settings, Depends(settings_dep)],
) -> RedirectResponse:
    client_id, _ = _require_google(settings)
    async with factory(user_id=None) as uow:
        st = await create_state(uow, purpose="signin", now=_now())
    url = authorization_url(
        client_id=client_id,
        redirect_uri=settings.google_signin_redirect_uri,
        state=st.state,
        code_challenge=st.code_challenge,
        nonce=st.nonce,
    )
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        STATE_COOKIE,
        st.state,
        max_age=600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/api/v1/auth/google",
    )
    return resp


@router.get("/auth/google/callback")
async def callback(
    request: Request,
    code: str,
    state: str,
    factory: Annotated[UnitOfWorkFactory, Depends(uow_factory)],
    settings: Annotated[Settings, Depends(settings_dep)],
    http: Annotated[httpx.AsyncClient, Depends(http_client)],
    keys: Annotated[JwksCache, Depends(jwks)],
) -> RedirectResponse:
    client_id, client_secret = _require_google(settings)
    if not secrets.compare_digest(request.cookies.get(STATE_COOKIE, ""), state):
        raise PermissionDenied("state does not belong to this browser")
    now = _now()
    async with factory(user_id=None) as uow:
        consumed = await consume_state(uow, state, purpose="signin", now=now)
    tokens = await exchange_code(
        http,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        code_verifier=consumed.code_verifier,
        redirect_uri=settings.google_signin_redirect_uri,
    )
    if not tokens.id_token:
        raise ValidationFailed("Google returned no ID token")
    identity = await verify_id_token(tokens.id_token, client_id=client_id, nonce=consumed.nonce, jwks=keys)
    async with factory(user_id=None) as uow:
        user_id = await find_signin_user(uow, sub=identity.sub, email=identity.email)
    created = user_id is None
    user_id = user_id or uuid7()
    ip = request.client.host if request.client else None
    async with factory(user_id=user_id) as uow:
        if created:
            await create_user(uow, email=identity.email, display_name=identity.name or identity.email)
            await create_self_person(uow, email=identity.email, display_name=identity.name or identity.email)
        await link_google_identity(uow, sub=identity.sub)
        session = await create_session(
            uow,
            ttl=datetime.timedelta(days=settings.session_ttl_days),
            now=now,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
        await record_audit(uow, "signin", ip=ip, metadata={"new_user": created})
    resp = RedirectResponse(settings.web_base_url, status_code=302)
    max_age = settings.session_ttl_days * 86400
    resp.set_cookie(
        settings.session_cookie_name,
        session.token,
        max_age=max_age,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    resp.set_cookie(
        settings.csrf_cookie_name,
        session.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    resp.delete_cookie(STATE_COOKIE, path="/api/v1/auth/google")
    return resp


@router.post("/auth/logout", status_code=204)
async def logout(
    user: Annotated[CurrentUser, Depends(current_user)],
    factory: Annotated[UnitOfWorkFactory, Depends(uow_factory)],
    settings: Annotated[Settings, Depends(settings_dep)],
) -> Response:
    async with factory(user_id=user.user_id) as uow:
        await revoke_session(uow, user.session_id, now=_now())
        await record_audit(uow, "logout")
    resp = Response(status_code=204)
    resp.delete_cookie(settings.session_cookie_name, path="/")
    resp.delete_cookie(settings.csrf_cookie_name, path="/")
    return resp


@router.get("/me")
async def me(
    user: Annotated[CurrentUser, Depends(current_user)],
    factory: Annotated[UnitOfWorkFactory, Depends(uow_factory)],
) -> dict[str, object]:
    from eca.connections import list_connections

    async with factory(user_id=user.user_id) as uow:
        profile = await get_profile(uow)
        connections = await list_connections(uow)
    return {
        "id": str(profile.user_id),
        "email": profile.email,
        "display_name": profile.display_name,
        "timezone": profile.timezone,
        "status": profile.status,
        "capabilities": sorted({cap for c in connections if c.status == "active" for cap in c.capabilities}),
    }


@router.delete("/me", status_code=202)
async def delete_me(
    user: Annotated[CurrentUser, Depends(current_user)],
    factory: Annotated[UnitOfWorkFactory, Depends(uow_factory)],
    settings: Annotated[Settings, Depends(settings_dep)],
) -> dict[str, str]:
    """Records the deletion request; the ``privacy`` job deletes the account. Needs a recent sign-in."""
    now = _now()
    if now - user.reauth_at > datetime.timedelta(minutes=settings.reauth_max_age_minutes):
        raise PermissionDenied("sign in again to delete your account", details={"reason": "reauth_required"})
    async with factory(user_id=user.user_id) as uow:
        job_id = await request_account_deletion(uow)
        await revoke_all_sessions(uow, now=now)
        await discard_user_states(uow, user.user_id)
        await record_audit(uow, "account_deletion_requested", target_type="deletion_job", target_id=job_id)
    return {"deletion_job_id": str(job_id), "status": "pending"}
