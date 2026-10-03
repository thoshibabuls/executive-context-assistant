"""Pipeline test harness (Batch A): tenants, fake connectors, inline execution.

Tenants are created only through the service functions (``identity.create_user`` and
``people.create_self_person`` in an API-role unit of work for the new user's own ID); there is no
authentication bypass. Handlers run through ``eca.platform.inline.InlineExecutor`` with the
production registry (``eca.worker.composition``: every domain handler, independent of what
earlier tests imported) and an AI client over ``tests.fake_ai.FakeProvider`` (no network).
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from sqlalchemy.ext.asyncio import AsyncEngine

from eca.connections import create_connection
from eca.connectors import ConnectorRegistry, FakeAccounts, FakeFeed, NormalizedMessage, load_eml_dir
from eca.identity import create_user
from eca.intelligence import AIClient
from eca.people import create_self_person
from eca.platform.clock import ManualClock
from eca.platform.db import create_engine, create_session_factory
from eca.platform.events import Resources
from eca.platform.ids import uuid7
from eca.platform.inline import InlineExecutor
from eca.platform.uow import UnitOfWorkFactory
from eca.worker.composition import production_registry
from tests.conftest import TempDatabase
from tests.fake_ai import FakeProvider, fake_ai_client

REPO = Path(__file__).resolve().parents[3]
WORLD_V1 = REPO / "evals" / "ai" / "datasets" / "world_v1"
WORLD_V1_USER = ("avery@brightwater.example", "Avery Lindqvist", "America/Los_Angeles")
T_START = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=datetime.UTC)  # after every world_v1 email


def world_v1_messages() -> list[NormalizedMessage]:
    return load_eml_dir(WORLD_V1 / "sources" / "emails", account_email=WORLD_V1_USER[0])


@dataclass
class Tenant:
    user_id: UUID
    connection_id: UUID
    email: str


@dataclass
class Pipeline:
    db: TempDatabase
    api: UnitOfWorkFactory
    worker: UnitOfWorkFactory
    engines: list[AsyncEngine]
    accounts: FakeAccounts
    connectors: ConnectorRegistry
    clock: ManualClock
    fake_ai: FakeProvider
    ai: AIClient
    extra_resources: list[object] = field(default_factory=list)

    @property
    def resources(self) -> Resources:
        return Resources.of(self.connectors, self.clock, self.ai, *self.extra_resources)

    def executor(self) -> InlineExecutor:
        return InlineExecutor(self.worker, production_registry(), self.resources)

    async def drain(self) -> int:
        return await self.executor().drain()

    async def tenant(
        self, email: str = WORLD_V1_USER[0], name: str = WORLD_V1_USER[1], timezone: str = WORLD_V1_USER[2]
    ) -> Tenant:
        user_id = uuid7()
        async with self.api(user_id=user_id) as uow:
            await create_user(uow, email=email, display_name=name, timezone=timezone)
            await create_self_person(uow, email=email, display_name=name)
            connection_id = await create_connection(uow, provider="fake", account_email=email)
        return Tenant(user_id=user_id, connection_id=connection_id, email=email)

    def feed(self, tenant: Tenant, *, page_size: int = 20) -> FakeFeed[NormalizedMessage]:
        return self.accounts.mail_feed(tenant.email, page_size=page_size)

    def rows(self, query: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.db.admin_url) as conn:
            return conn.execute(query, params).fetchall()  # type: ignore[arg-type]

    def scalar(self, query: str, params: Sequence[Any] = ()) -> Any:
        return self.rows(query, params)[0][0]


@asynccontextmanager
async def pipeline(db: TempDatabase, *, start: datetime.datetime = T_START) -> AsyncIterator[Pipeline]:
    api_engine = create_engine(db.runtime_url, pool_size=4, max_overflow=0)
    worker_engine = create_engine(db.worker_url, pool_size=6, max_overflow=0)
    accounts = FakeAccounts()
    connectors = ConnectorRegistry()
    connectors.register_mail("fake", accounts.mail_connector)
    connectors.register_calendar("fake", accounts.calendar_connector)
    fake = FakeProvider()
    worker = UnitOfWorkFactory(create_session_factory(worker_engine))
    p = Pipeline(
        db=db,
        api=UnitOfWorkFactory(create_session_factory(api_engine)),
        worker=worker,
        engines=[api_engine, worker_engine],
        accounts=accounts,
        connectors=connectors,
        clock=ManualClock(start),
        fake_ai=fake,
        ai=fake_ai_client(fake, uow_factory=worker),
    )
    try:
        yield p
    finally:
        for engine in p.engines:
            await engine.dispose()


# Natural-key state hash over the normalize-level tables (no generated IDs), for RT-04.
_STATE_QUERIES = {
    "source_items": "SELECT external_id, encode(content_hash, 'hex'), stage, coalesce(last_error_code, '') "
    "FROM source_items WHERE user_id = %(u)s ORDER BY external_id",
    "messages": "SELECT s.external_id, m.direction, m.is_bulk, coalesce(m.prefilter_reason, ''), "
    "m.body_clean, p.primary_email FROM messages m JOIN source_items s ON s.id = m.source_item_id "
    "LEFT JOIN persons p ON p.id = m.sender_person_id WHERE m.user_id = %(u)s ORDER BY s.external_id",
    "participants": "SELECT s.external_id, p.primary_email, mp.role FROM message_participants mp "
    "JOIN messages m ON m.id = mp.message_id JOIN source_items s ON s.id = m.source_item_id "
    "JOIN persons p ON p.id = mp.person_id WHERE mp.user_id = %(u)s ORDER BY 1, 2, 3",
    "persons": "SELECT primary_email, coalesce(display_name, ''), is_self, coalesce(o.domain::text, ''), "
    "first_seen_at, last_interaction_at, last_inbound_at, last_outbound_at FROM persons p "
    "LEFT JOIN organizations o ON o.id = p.organization_id WHERE p.user_id = %(u)s ORDER BY primary_email",
    "identifiers": "SELECT p.primary_email, i.kind, i.value_normalized FROM person_identifiers i "
    "JOIN persons p ON p.id = i.person_id WHERE i.user_id = %(u)s ORDER BY 1, 2, 3",
    "conversations": "SELECT external_thread_id, subject, first_message_at, last_message_at, "
    "last_inbound_at, last_outbound_at, awaiting, needs_reply, coalesce(needs_reply_source, '') "
    "FROM conversations WHERE user_id = %(u)s ORDER BY external_thread_id",
}


def state_hash(p: Pipeline, user_id: UUID, queries: dict[str, str] | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    with psycopg.connect(p.db.admin_url) as conn:
        for name, query in (queries or _STATE_QUERIES).items():
            rows = conn.execute(query, {"u": user_id}).fetchall()  # type: ignore[arg-type]
            out[name] = hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()
    return out
