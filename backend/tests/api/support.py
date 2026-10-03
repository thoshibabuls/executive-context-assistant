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

from eca.api.app import create_app
from eca.identity import create_session, create_user
from eca.people import create_self_person, resolve_address
from eca.platform.clock import SystemClock
from eca.platform.config import Settings
from eca.platform.db import create_engine, create_session_factory
from eca.platform.events import Resources
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


async def _drain(h: ApiHarness) -> int:
    engine = create_engine(h.db.worker_url, pool_size=4, max_overflow=0)
    try:
        factory = UnitOfWorkFactory(create_session_factory(engine))
        if h.ai_mode == "replay":
            assert h.cassette_dir is not None
            client = replay_ai_client(h.cassette_dir, uow_factory=factory)
        else:
            record_to = h.cassette_dir if h.ai_mode == "record" else None
            client = fake_ai_client(h.fake_ai, uow_factory=factory, cassette_dir=record_to)
        resources = Resources.of(LocalObjectStorage(h.objects, storage_signer()), client, SystemClock())
        return await InlineExecutor(factory, production_registry(), resources).drain()
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
        yield ApiHarness(db=db, client=client, objects=objects)
