"""Slice 1.3 on PostgreSQL: sync (fake connector) → source items → normalize → prefilter."""

from __future__ import annotations

import asyncio
import datetime
import json
import random
from dataclasses import replace

import pytest

from eca.connectors import FakeConnectorFailure, FakeFeed, NormalizedMessage, NormalizedPerson
from eca.ingestion import SYNC_REQUESTED, SyncRequested, reconcile_user_stages, sync_mail
from eca.people import find_by_email, merge_persons, merged_ids
from eca.platform.events import NewEvent
from eca.platform.outbox import publish
from tests.conftest import TempDatabase, new_database
from tests.pipeline.support import WORLD_V1, Pipeline, Tenant, pipeline, state_hash, world_v1_messages

pytestmark = pytest.mark.db


async def _sync(p: Pipeline, t: Tenant, owner: str = "test") -> None:
    await sync_mail(
        p.worker,
        p.connectors,
        user_id=t.user_id,
        connection_id=t.connection_id,
        now=p.clock.now(),
        owner=owner,
    )


async def _world(p: Pipeline) -> Tenant:
    t = await p.tenant()
    p.feed(t).extend(world_v1_messages())
    await _sync(p, t)
    return t


async def test_world_v1_sync_and_normalize(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        t = await _world(p)
        assert p.scalar("SELECT count(*) FROM source_items") == 150
        assert p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'SourceItemStored'") == 150
        assert p.rows("SELECT cursor, import_state, lease_owner FROM sync_cursors") == [("150", "done", None)]
        await p.drain()
        assert p.scalar("SELECT count(*) FROM messages") == 150
        stages = dict(p.rows("SELECT stage, count(*) FROM source_items GROUP BY stage"))
        assert set(stages) <= {"skipped", "extract_pending", "normalized"}
        assert (
            p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'MessageNormalized'")
            == stages["extract_pending"]
        )
        assert p.scalar("SELECT count(*) FROM persons WHERE is_self") == 1
        # Outbound mail is the user's own: direction outbound, never prefiltered.
        assert (
            p.scalar(
                "SELECT count(*) FROM messages m JOIN source_items s ON s.id = m.source_item_id "
                "WHERE m.direction = 'outbound' AND s.stage = 'skipped'"
            )
            == 0
        )
        assert t.user_id


def _labels() -> dict[str, dict[str, object]]:
    rows = (WORLD_V1 / "labels" / "triage.jsonl").read_text(encoding="utf-8").splitlines()
    return {r["case_id"]: r for r in map(json.loads, rows)}


async def test_e1_prefilter_false_skip_rate_on_world_v1(isolated_db: TempDatabase) -> None:
    """E1 (AI_EVALUATION.md §4.1): relevant mail skipped by rules ≤ 2% overall, 0% for VIPs.

    Deterministic rules against the (draft) world_v1 labels; no model is involved.
    """
    async with pipeline(isolated_db) as p:
        await _world(p)
        await p.drain()
        stage = dict(p.rows("SELECT external_id, stage FROM source_items"))
    labels = _labels()
    relevant = [c for c, row in labels.items() if not row["prefilter_skip"]]
    irrelevant = [c for c, row in labels.items() if row["prefilter_skip"]]
    false_skips = [c for c in relevant if stage[c] == "skipped"]
    missed_skips = [c for c in irrelevant if stage[c] != "skipped"]
    report = {
        "relevant": len(relevant),
        "false_skips": false_skips,
        "bulk": len(irrelevant),
        "missed": missed_skips,
    }
    assert len(false_skips) / len(relevant) <= 0.02, report
    vip_false = [c for c in false_skips if labels[c]["sender_type"] == "vip"]
    assert vip_false == [], report
    assert missed_skips == [], report  # every newsletter and notification is prefiltered (no AI-01 call)


async def _redeliver_all(p: Pipeline, event_type: str) -> None:
    """Replay: publish every event of a type again as a new outbox row, then let it be dispatched.

    The new rows have new event IDs, so ``event_consumptions`` does not absorb them; idempotency
    must come from the stage functions themselves.
    """
    from eca.platform.events import default_registry

    rows = p.rows(
        "SELECT user_id, event_type, aggregate_type, aggregate_id, payload FROM outbox "
        "WHERE event_type = %s ORDER BY created_at, id",
        (event_type,),
    )
    model = default_registry.payload_model(event_type)
    for user_id, etype, agg_type, agg_id, payload in rows:
        async with p.worker(user_id=user_id) as uow:
            await publish(uow, NewEvent(etype, agg_type, agg_id, model.model_validate(payload)))


async def test_normalize_is_idempotent_on_redelivery(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        t = await _world(p)
        await p.drain()
        before = state_hash(p, t.user_id)
        normalized = p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'MessageNormalized'")
        await _redeliver_all(p, "SourceItemStored")
        await p.drain()
        assert state_hash(p, t.user_id) == before
        assert p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'MessageNormalized'") == normalized


async def test_rt04_replay_three_times_in_order_and_shuffled(admin_base_url: str) -> None:
    """RT-04 (slice 1.3 level): same source chain delivered in order and shuffled gives one state.

    Three fresh tenants ingest world_v1 in sent order and in two shuffled delivery orders; then each
    replays the whole chain three more times (re-sync from scratch and re-delivered events).
    """
    hashes = []
    messages = world_v1_messages()
    for seed in (None, 1, 2):
        order = list(messages)
        if seed is not None:
            random.Random(seed).shuffle(order)
        cm = new_database(admin_base_url, migrate=True)
        db = await asyncio.to_thread(cm.__enter__)  # Alembic runs its own event loop
        try:
            async with pipeline(db) as p:
                t = await p.tenant()
                feed = p.feed(t, page_size=17)
                for m in order:
                    feed.add(m)  # same delivery time: shuffled order is the delivery order
                await _sync(p, t)
                await p.drain()
                first = state_hash(p, t.user_id)
                for replay in range(3):
                    replayed = list(order)
                    random.Random(100 + replay).shuffle(replayed)
                    p.accounts.mail[t.email] = FakeFeed(page_size=13)
                    p.accounts.mail[t.email].extend(replayed)
                    p.rows("UPDATE sync_cursors SET cursor = NULL RETURNING 1")
                    await _sync(p, t, owner=f"replay-{replay}")
                    await _redeliver_all(p, "SourceItemStored")
                    await p.drain()
                    assert state_hash(p, t.user_id) == first
                hashes.append(first)
                assert p.scalar("SELECT count(*) FROM source_items") == 150
        finally:
            await asyncio.to_thread(cm.__exit__, None, None, None)
    assert hashes[0] == hashes[1] == hashes[2]


async def test_rt06_crash_mid_sync_resumes_without_gaps_or_duplicates(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        t = await p.tenant()
        feed = p.feed(t, page_size=20)
        feed.extend(world_v1_messages())
        feed.fail_after_pages = 3
        with pytest.raises(FakeConnectorFailure):
            await _sync(p, t)
        # Three pages committed; the cursor did not move; the page token is the resume point.
        assert p.scalar("SELECT count(*) FROM source_items") == 60
        assert p.rows(
            "SELECT cursor, import_page_token, import_state, lease_owner, consecutive_failures "
            "FROM sync_cursors"
        ) == [(None, "60", "running", None, 1)]
        await _sync(p, t, owner="second-run")
        assert p.scalar("SELECT count(*) FROM source_items") == 150
        assert p.scalar("SELECT count(DISTINCT external_id) FROM source_items") == 150
        assert p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'SourceItemStored'") == 150
        assert p.rows("SELECT cursor, import_page_token, import_state FROM sync_cursors") == [
            ("150", None, "done")
        ]


async def test_expired_cursor_triggers_bounded_resync(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        t = await _world(p)
        extra = replace(world_v1_messages()[0], external_id="EXTRA-1", rfc822_id="<extra-1@world-v1.example>")
        p.feed(t).add(extra)
        p.feed(t).expire_cursors_below = 1000
        await _sync(p, t, owner="after-expiry")
        assert p.scalar("SELECT count(*) FROM source_items") == 151
        assert p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'SourceItemStored'") == 151


class _SlowConnector:
    def __init__(self, inner: object) -> None:
        self.inner = inner

    async def list_messages(self, **kw: object) -> object:
        await asyncio.sleep(0.05)
        return await self.inner.list_messages(**kw)  # type: ignore[attr-defined]


async def test_rt07_duplicate_sync_triggers_give_one_run_and_one_set_of_items(
    isolated_db: TempDatabase,
) -> None:
    async with pipeline(isolated_db) as p:
        t = await p.tenant()
        p.feed(t).extend(world_v1_messages())
        p.connectors.register_mail("fake", lambda info: _SlowConnector(p.accounts.mail_connector(info)))  # type: ignore[arg-type,return-value]
        reports = await asyncio.gather(
            *(
                sync_mail(
                    p.worker,
                    p.connectors,
                    user_id=t.user_id,
                    connection_id=t.connection_id,
                    now=p.clock.now(),
                    owner=f"trigger-{name}",
                )
                for name in ("manual", "periodic", "webhook")
            )
        )
        assert sorted(r.ran for r in reports) == [False, False, True]
        # The same three triggers as outbox events, run through the real handler.
        async with p.worker(user_id=t.user_id) as uow:
            for trigger in ("manual", "periodic", "webhook"):
                await publish(
                    uow,
                    NewEvent(
                        SYNC_REQUESTED,
                        "connection",
                        t.connection_id,
                        SyncRequested(connection_id=t.connection_id, resource="mail", trigger=trigger),
                    ),
                )
        await p.drain()
        assert p.scalar("SELECT count(*) FROM source_items") == 150
        assert p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'SourceItemStored'") == 150
        assert p.scalar("SELECT count(*) FROM messages") == 150


async def test_duplicate_rfc822_under_another_provider_id_becomes_an_alias(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        t = await p.tenant()
        original = world_v1_messages()[1]
        copy = replace(original, external_id="COPY-OF-" + original.external_id)
        p.feed(t).extend([original, copy])
        await _sync(p, t)
        await p.drain()
        assert p.scalar("SELECT count(*) FROM messages") == 1
        assert p.scalar("SELECT cardinality(alias_source_item_ids) FROM messages") == 1
        assert (
            dict(p.rows("SELECT external_id, stage FROM source_items"))["COPY-OF-" + original.external_id]
            == "normalized"
        )


async def test_reconciler_republishes_items_stuck_past_their_sla(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        t = await p.tenant()
        p.feed(t).extend(world_v1_messages()[:5])
        await _sync(p, t)
        # Simulate lost jobs: events dispatched, handlers never ran; items stuck in 'fetched'.
        p.rows("UPDATE outbox SET status = 'dispatched' RETURNING 1")
        p.rows("UPDATE source_items SET stage_updated_at = now() - interval '10 minutes' RETURNING 1")
        now = datetime.datetime.now(datetime.UTC)
        async with p.worker(user_id=t.user_id) as uow:
            assert await reconcile_user_stages(uow, now=now) == 5
        async with p.worker(user_id=t.user_id) as uow:
            assert await reconcile_user_stages(uow, now=now) == 0  # next_attempt_at holds the item back
        await p.drain()
        assert p.scalar("SELECT count(*) FROM source_items WHERE stage = 'fetched'") == 0
        assert p.scalar("SELECT count(*) FROM messages") == 5


async def test_reconciler_moves_items_to_needs_attention_after_the_attempt_cap(
    isolated_db: TempDatabase,
) -> None:
    async with pipeline(isolated_db) as p:
        t = await p.tenant()
        p.feed(t).extend(world_v1_messages()[:1])
        await _sync(p, t)
        p.rows(
            "UPDATE source_items SET stage_updated_at = now() - interval '1 hour', stage_attempts = 7 "
            "RETURNING 1"
        )
        async with p.worker(user_id=t.user_id) as uow:
            await reconcile_user_stages(uow, now=datetime.datetime.now(datetime.UTC))
        assert p.rows("SELECT stage, last_error_code FROM source_items") == [
            ("needs_attention", "stage_sla_exhausted")
        ]


def _msg(
    ext: str,
    sender: tuple[str, str],
    to: list[tuple[str, str]],
    body: str,
    minute: int,
    *,
    reply_to: str | None = None,
    thread: str | None = None,
) -> NormalizedMessage:
    rfc = f"<{ext.lower()}@test.example>"
    return NormalizedMessage(
        external_id=ext,
        thread_external_id=thread or rfc,
        rfc822_id=rfc,
        in_reply_to=reply_to,
        sent_at=datetime.datetime(2026, 9, 21, 9, minute, tzinfo=datetime.UTC),
        sender=NormalizedPerson(sender[1], sender[0]),
        to=tuple(NormalizedPerson(e, n) for n, e in to),
        cc=(),
        subject="Numbers",
        body_text=body,
        body_html=None,
        categories=("sent",) if sender[1] == "avery@brightwater.example" else ("inbox",),
    )


AVERY = ("Avery Lindqvist", "avery@brightwater.example")


async def test_identity_resolution_never_auto_merges(isolated_db: TempDatabase) -> None:
    """CC-41 and CC-42 at the normalize level (TECHNICAL_DESIGN.md §13.7)."""
    async with pipeline(isolated_db) as p:
        t = await p.tenant()
        p.feed(t).extend(
            [
                _msg("A1", ("Alex Rivera", "alex.rivera@kestrelbank.example"), [AVERY], "Hi", 1),
                _msg("A2", ("Alex Rivera", "alex.r@gmail.com"), [AVERY], "Hi from home", 2),
                _msg("A3", ("Jon Smith", "jon.smith@tallgrass.example"), [AVERY], "Vendor note", 3),
                _msg("A4", ("John Smyth", "john.smyth@brightwater.example"), [AVERY], "Internal note", 4),
                _msg("A5", ("Alex", "Alex.R+news@googlemail.com"), [AVERY], "Gmail dots and plus", 5),
            ]
        )
        await _sync(p, t)
        await p.drain()
        emails = sorted(r[0] for r in p.rows("SELECT primary_email FROM persons WHERE NOT is_self"))
        assert emails == [
            "alex.rivera@kestrelbank.example",
            "alexr@gmail.com",
            "john.smyth@brightwater.example",
            "jon.smith@tallgrass.example",
        ]
        assert dict(p.rows("SELECT name, domain::text FROM organizations ORDER BY 1")) == {
            "Brightwater": "brightwater.example",
            "Kestrelbank": "kestrelbank.example",
            "Tallgrass": "tallgrass.example",
        }
        async with p.api(user_id=t.user_id) as uow:
            work = await find_by_email(uow, "alex.rivera@kestrelbank.example")
            home = await find_by_email(uow, "alex.r@gmail.com")
            assert work and home and work.id != home.id
            await merge_persons(uow, source_id=home.id, target_id=work.id)
        async with p.api(user_id=t.user_id) as uow:
            assert (await find_by_email(uow, "alexr@gmail.com")).id == work.id  # type: ignore[union-attr]
            assert await merged_ids(uow, work.id) == sorted([work.id, home.id])


async def test_conversation_reply_state_follows_the_latest_message(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        t = await p.tenant()
        raj = ("Raj Menon", "raj.menon@kestrelbank.example")
        first = _msg("R1", raj, [AVERY], "Could you send the numbers?", 1)
        p.feed(t).add(first)
        await _sync(p, t)
        await p.drain()
        assert p.rows("SELECT awaiting, needs_reply, needs_reply_source FROM conversations") == [
            ("user", True, "heuristic")
        ]
        p.feed(t).add(
            _msg("R2", AVERY, [raj], "Sure, attached.", 2, reply_to=first.rfc822_id, thread=first.rfc822_id)
        )
        await _sync(p, t, owner="second")
        await p.drain()
        assert p.rows("SELECT awaiting, needs_reply FROM conversations") == [("other", False)]
        assert p.scalar("SELECT count(*) FROM messages WHERE direction = 'outbound'") == 1
