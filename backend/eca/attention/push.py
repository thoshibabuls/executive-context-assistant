"""Push subscriptions and the Web Push delivery job (BACKEND_DESIGN.md §10.1, §15, §16.8).

``send_push`` is the ``attention.web_push`` natural-key handler body: transaction 1 claims the
notification (``pending → sending``, attempts + 1) and reads the active subscriptions; the HTTP
calls run outside any transaction; transaction 2 records ``sent`` (or ``failed``), revokes
subscriptions the push service reports gone, and leaves retryable failures in ``sending`` for the
sweep (at most 3 attempts). A crash after the push service accepted but before ``sent`` is stored
can repeat one push; the Topic header collapses it while undelivered.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from eca.attention.models import notifications_table, push_subscriptions_table
from eca.attention.webpush import PushResult, WebPushSender, allowed_endpoint, topic_for
from eca.platform.errors import NotFound, ValidationFailed
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger("eca.attention.push")

MAX_SUBSCRIPTIONS = 10


@dataclass(frozen=True)
class SubscriptionView:
    id: UUID
    endpoint_host: str
    created_at: datetime.datetime | None
    expires_at: datetime.datetime | None


async def subscribe(
    uow: UnitOfWork, *, endpoint: str, expires_at: datetime.datetime | None, user_agent: str | None
) -> SubscriptionView:
    if not allowed_endpoint(endpoint):
        raise ValidationFailed("endpoint must be an https URL of a known push service")
    s = push_subscriptions_table
    active = (
        await uow.session.execute(
            select(s.c.id).where(
                s.c.user_id == uow.user_id, s.c.revoked_at.is_(None), s.c.endpoint != endpoint
            )
        )
    ).all()
    if len(active) >= MAX_SUBSCRIPTIONS:
        raise ValidationFailed(f"at most {MAX_SUBSCRIPTIONS} active push subscriptions")
    row = (
        await uow.session.execute(
            insert(s)
            .values(
                id=uuid7(),
                user_id=uow.user_id,
                endpoint=endpoint,
                expires_at=expires_at,
                user_agent=(user_agent or "")[:200] or None,
                failure_count=0,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "endpoint"],
                set_={"revoked_at": None, "expires_at": expires_at, "failure_count": 0},
            )
            .returning(s.c.id, s.c.endpoint, s.c.created_at, s.c.expires_at)
        )
    ).one()
    return SubscriptionView(row.id, _host(row.endpoint), row.created_at, row.expires_at)


def _host(endpoint: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(endpoint).hostname or ""


async def unsubscribe(uow: UnitOfWork, subscription_id: UUID, *, now: datetime.datetime) -> None:
    s = push_subscriptions_table
    found = (
        await uow.session.execute(select(s.c.id).where(s.c.user_id == uow.user_id, s.c.id == subscription_id))
    ).one_or_none()
    if found is None:
        raise NotFound("push subscription not found")
    await uow.session.execute(
        update(s).where(s.c.id == subscription_id, s.c.revoked_at.is_(None)).values(revoked_at=now)
    )


async def send_push(
    factory: UnitOfWorkFactory,
    sender: WebPushSender | None,
    *,
    user_id: UUID,
    reminder_id: UUID,
    seq: int,
    now: datetime.datetime,
) -> str:
    n, s = notifications_table, push_subscriptions_table
    async with factory(user_id=user_id) as uow:
        claimed = (
            await uow.session.execute(
                update(n)
                .where(
                    n.c.user_id == user_id,
                    n.c.reminder_id == reminder_id,
                    n.c.channel == "web_push",
                    n.c.seq == seq,
                    n.c.state == "pending",
                )
                .values(state="sending", attempts=n.c.attempts + 1)
                .returning(n.c.id)
            )
        ).scalar_one_or_none()
        if claimed is None:
            return "skipped"  # already sent, sending elsewhere, or failed
        if sender is None:
            await uow.session.execute(
                update(n).where(n.c.id == claimed).values(state="failed", last_error="not_configured")
            )
            return "not_configured"
        subs = (
            await uow.session.execute(
                select(s.c.id, s.c.endpoint).where(s.c.user_id == user_id, s.c.revoked_at.is_(None))
            )
        ).all()
    results: list[tuple[UUID, PushResult]] = []
    for sub in subs:
        results.append((sub.id, await sender.send(sub.endpoint, topic=topic_for(reminder_id), now=now)))
    sent = any(r.status == "sent" for _, r in results)
    retry = any(r.status == "retry" for _, r in results)
    async with factory(user_id=user_id) as uow:
        for sub_id, result in results:
            if result.status == "sent":
                await uow.session.execute(
                    update(s).where(s.c.id == sub_id).values(last_success_at=now, failure_count=0)
                )
            elif result.status == "gone":
                await uow.session.execute(
                    update(s).where(s.c.id == sub_id, s.c.revoked_at.is_(None)).values(revoked_at=now)
                )
            else:
                await uow.session.execute(
                    update(s).where(s.c.id == sub_id).values(failure_count=s.c.failure_count + 1)
                )
        if sent:
            outcome, values = "sent", {"state": "sent", "sent_at": now, "last_error": None}
        elif retry:
            outcome, values = "retry", {"last_error": "retryable"}  # stays sending; the sweep retries
        else:
            outcome, values = "failed", {"state": "failed", "last_error": "no_subscription"}
        await uow.session.execute(update(n).where(n.c.id == claimed, n.c.state == "sending").values(**values))
    log.info("web_push", outcome=outcome, subscriptions=len(results))
    return outcome
