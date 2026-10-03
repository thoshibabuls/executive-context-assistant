"""Authenticated API test harness (Phase 4 deferred API tests).

Users and sessions are created through the service functions (``identity.create_user``,
``people.create_self_person``, ``identity.create_session``) in an API-role unit of work for the new
user's own ID; requests carry the session cookie and the CSRF header exactly like the web app.
Background work runs through ``InlineExecutor`` with the production registry, the same local
object storage as the API and an AI client over ``tests.fake_ai.FakeProvider`` (no network).
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import text

from eca.api.app import create_app
from eca.connections import create_connection
from eca.connectors import ConnectorRegistry, FakeAccounts, NormalizedEvent, NormalizedMessage
from eca.identity import create_session, create_user
from eca.ingestion import sync_calendar, sync_mail
from eca.people import create_self_person, resolve_address
from eca.platform.clock import SystemClock
from eca.platform.config import Settings
from eca.platform.db import create_engine, create_session_factory
from eca.platform.events import EventEnvelope, Resources
from eca.platform.ids import uuid7
from eca.platform.inline import InlineExecutor
from eca.platform.storage import LocalObjectStorage, UploadSigner
from eca.platform.uow import UnitOfWorkFactory
from eca.worker.composition import production_registry
from tests.conftest import TempDatabase
from tests.fake_ai import FakeProvider, fake_ai_client, replay_ai_client

SIGNING_SECRET = "test-storage-signing-secret-0123456789"  # test-only value


@dataclass
class ApiUser:
    user_id: UUID
    email: str
    cookies: dict[str, str]
    csrf: str
    self_person_id: UUID

    def headers(self, **extra: str) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf, **extra}


@dataclass
class ApiHarness:
    db: TempDatabase
    client: TestClient
    objects: Path
    fake_ai: FakeProvider = field(default_factory=FakeProvider)
    # AI mode of the inline worker: the fake provider ("live"), the fake provider writing cassettes
    # ("record"), or cassettes only ("replay"; a miss raises CassetteMiss).
    ai_mode: str = "live"
    cassette_dir: Path | None = None
    accounts: FakeAccounts = field(default_factory=FakeAccounts)

    @property
    def connectors(self) -> ConnectorRegistry:
        registry = ConnectorRegistry()
        registry.register_mail("fake", self.accounts.mail_connector)
        registry.register_calendar("fake", self.accounts.calendar_connector)
        return registry

    def connect_mail(self, user: ApiUser, messages: Sequence[NormalizedMessage]) -> UUID:
        """A fake mail connection for ``user`` with these messages, synced once (no drain)."""
        self.accounts.mail_feed(user.email).extend(messages)
        return asyncio.run(_connect_and_sync(self, user))

    def request(self, user: ApiUser, method: str, url: str, **kwargs: Any) -> Any:
        headers = {**user.headers(), **kwargs.pop("headers", {})}
        self.client.cookies.clear()
        for name, value in user.cookies.items():
            self.client.cookies.set(name, value)
        return self.client.request(method, url, headers=headers, **kwargs)

    def user(self, email: str, name: str, timezone: str = "America/Los_Angeles") -> ApiUser:
        return asyncio.run(_new_user(self.db, email, name, timezone))

    def person(self, user: ApiUser, email: str, name: str) -> UUID:
        """A known contact of ``user`` (as if seen in a mail header)."""
        return asyncio.run(_new_person(self.db, user.user_id, email, name))

    def drain(self) -> int:
        """Run every pending event through the production handlers (the worker's job)."""
        return asyncio.run(_drain(self))

    def sync_mail_again(self, user: ApiUser) -> None:
        """Sync the user's fake mailbox again over the existing connection (new mail, deletions)."""
        asyncio.run(_sync_mail(self, user))

    def sync_calendar(self, user: ApiUser, events: Sequence[NormalizedEvent]) -> None:
        """Add events to the user's fake calendar and sync it over the existing fake connection."""
        self.accounts.calendar_feed(user.email).extend(events)
        asyncio.run(_sync_calendar(self, user))

    def redeliver(self, event_types: Sequence[str]) -> int:
        """Deliver every already dispatched event of these types again (duplicate dispatch, RT-08)."""
        return asyncio.run(_redeliver(self, list(event_types)))

    def redeliver_rows(self, rows: Sequence[tuple[Any, ...]]) -> None:
        """Run outbox rows (id, user_id, event_type, aggregate_type, aggregate_id, payload,
        correlation, created_at) through their handlers, as a job queued earlier would."""
        asyncio.run(_run_rows(self, rows))

    def rows(self, query: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.db.admin_url) as conn:
            return conn.execute(query, params).fetchall()  # type: ignore[arg-type]

    def scalar(self, query: str, params: Sequence[Any] = ()) -> Any:
        return self.rows(query, params)[0][0]


def storage_signer() -> UploadSigner:
    return UploadSigner(hashlib.sha256(SIGNING_SECRET.encode()).digest())


async def _new_user(db: TempDatabase, email: str, name: str, timezone: str) -> ApiUser:
    engine = create_engine(db.runtime_url, pool_size=1, max_overflow=0)
    try:
        factory = UnitOfWorkFactory(create_session_factory(engine))
        user_id = uuid7()
        async with factory(user_id=user_id) as uow:
            await create_user(uow, email=email, display_name=name, timezone=timezone)
            person = await create_self_person(uow, email=email, display_name=name)
            session = await create_session(
                uow,
                ttl=datetime.timedelta(days=1),
                now=datetime.datetime.now(datetime.UTC),
                ip=None,
                user_agent="pytest",
            )
    finally:
        await engine.dispose()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    person_id = person if isinstance(person, UUID) else person.id
    return ApiUser(
        user_id=user_id,
        email=email,
        cookies={settings.session_cookie_name: session.token, settings.csrf_cookie_name: session.csrf_token},
        csrf=session.csrf_token,
        self_person_id=person_id,
    )


async def _new_person(db: TempDatabase, user_id: UUID, email: str, name: str) -> UUID:
    engine = create_engine(db.runtime_url, pool_size=1, max_overflow=0)
    try:
        factory = UnitOfWorkFactory(create_session_factory(engine))
        async with factory(user_id=user_id) as uow:
            ref = await resolve_address(uow, email=email, display_name=name)
        return ref.id
    finally:
        await engine.dispose()


async def _connect_and_sync(h: ApiHarness, user: ApiUser) -> UUID:
    api = create_engine(h.db.runtime_url, pool_size=1, max_overflow=0)
    worker = create_engine(h.db.worker_url, pool_size=2, max_overflow=0)
    try:
        async with UnitOfWorkFactory(create_session_factory(api))(user_id=user.user_id) as uow:
            connection_id = await create_connection(uow, provider="fake", account_email=user.email)
        await sync_mail(
            UnitOfWorkFactory(create_session_factory(worker)),
            h.connectors,
            user_id=user.user_id,
            connection_id=connection_id,
            now=datetime.datetime.now(datetime.UTC),
            owner="api-harness",
        )
        return connection_id
    finally:
        await api.dispose()
        await worker.dispose()


async def _sync_mail(h: ApiHarness, user: ApiUser) -> None:
    worker = create_engine(h.db.worker_url, pool_size=2, max_overflow=0)
    try:
        connection_id = h.scalar("SELECT id FROM connections WHERE user_id = %s", (user.user_id,))
        await sync_mail(
            UnitOfWorkFactory(create_session_factory(worker)),
            h.connectors,
            user_id=user.user_id,
            connection_id=connection_id,
            now=datetime.datetime.now(datetime.UTC),
            owner=f"api-harness-{uuid7()}",
        )
    finally:
        await worker.dispose()


async def _sync_calendar(h: ApiHarness, user: ApiUser) -> None:
    worker = create_engine(h.db.worker_url, pool_size=2, max_overflow=0)
    try:
        connection_id = h.scalar("SELECT id FROM connections WHERE user_id = %s", (user.user_id,))
        await sync_calendar(
            UnitOfWorkFactory(create_session_factory(worker)),
            h.connectors,
            user_id=user.user_id,
            connection_id=connection_id,
            now=datetime.datetime.now(datetime.UTC),
            owner="api-harness-calendar",
        )
    finally:
        await worker.dispose()


def _resources(h: ApiHarness, factory: UnitOfWorkFactory) -> Resources:
    if h.ai_mode == "replay":
        assert h.cassette_dir is not None
        client = replay_ai_client(h.cassette_dir, uow_factory=factory)
    else:
        record_to = h.cassette_dir if h.ai_mode == "record" else None
        client = fake_ai_client(h.fake_ai, uow_factory=factory, cassette_dir=record_to)
    return Resources.of(LocalObjectStorage(h.objects, storage_signer()), client, SystemClock(), h.connectors)


async def _drain(h: ApiHarness) -> int:
    engine = create_engine(h.db.worker_url, pool_size=4, max_overflow=0)
    try:
        factory = UnitOfWorkFactory(create_session_factory(engine))
        return await InlineExecutor(factory, production_registry(), _resources(h, factory)).drain()
    finally:
        await engine.dispose()


async def _redeliver(h: ApiHarness, event_types: list[str]) -> int:
    engine = create_engine(h.db.worker_url, pool_size=4, max_overflow=0)
    try:
        factory = UnitOfWorkFactory(create_session_factory(engine))
        executor = InlineExecutor(factory, production_registry(), _resources(h, factory))
        async with factory(user_id=None) as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            "SELECT id, user_id, event_type, aggregate_type, aggregate_id, payload, "
                            "correlation, "
                            "created_at FROM outbox WHERE status = 'dispatched' AND event_type = ANY(:types) "
                            "ORDER BY created_at, id"
                        ),
                        {"types": event_types},
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            await executor.run_envelope(EventEnvelope.model_validate(dict(row)).model_dump(mode="json"))
        await executor.drain()
        return len(rows)
    finally:
        await engine.dispose()


_ENVELOPE_FIELDS = (
    "id",
    "user_id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "payload",
    "correlation",
    "created_at",
)


async def _run_rows(h: ApiHarness, rows: Sequence[tuple[Any, ...]]) -> None:
    engine = create_engine(h.db.worker_url, pool_size=4, max_overflow=0)
    try:
        factory = UnitOfWorkFactory(create_session_factory(engine))
        executor = InlineExecutor(factory, production_registry(), _resources(h, factory))
        for row in rows:
            envelope = EventEnvelope.model_validate(dict(zip(_ENVELOPE_FIELDS, row, strict=True)))
            await executor.run_envelope(envelope.model_dump(mode="json"))
    finally:
        await engine.dispose()


@contextmanager
def api_harness(db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    objects = tmp_path / "objects"
    cassettes = tmp_path / "cassettes"
    cassettes.mkdir(parents=True, exist_ok=True)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_env="test",
        api_database_url=db.runtime_url,
        api_storage_local_dir=str(objects),
        storage_signing_key=SIGNING_SECRET,
        api_ai_mode="replay",
        api_ai_cassette_dir=str(cassettes),
        session_cookie_secure=False,
    )
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=True) as client:
        harness = ApiHarness(db=db, client=client, objects=objects)
        # The API's interactive AI calls (chat, reply guidance, topics) use the same fake provider.
        app.state.ai_client = fake_ai_client(harness.fake_ai, uow_factory=app.state.uow_factory)
        yield harness
