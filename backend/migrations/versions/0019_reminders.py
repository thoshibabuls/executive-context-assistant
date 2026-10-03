"""Slice 3.1: ``reminders`` and ``notifications`` (``attention`` module).

BACKEND_DESIGN.md §17.6 (DDL), §10.1 (idempotency: ``ux_reminders_fp`` and
``ux_notifications (reminder_id, channel, seq)``), §6.2 (COMPUTED rows with user state
transitions); TECHNICAL_DESIGN.md §15.4 (rules, keys, states). Business tables under per-user RLS
with the default DML grants of both runtime roles (§7.6, "Phase 3 tables"). No cascades.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("reminders", "notifications")


def _roles() -> tuple[str, str]:
    api = context.config.attributes.get("runtime_role", "eca_app")
    worker = context.config.attributes.get("worker_role", "eca_worker")
    assert isinstance(api, str) and isinstance(worker, str)
    return validate_identifier(api), validate_identifier(worker)


def upgrade() -> None:
    api, worker = _roles()
    check_runtime_roles(op.get_bind(), api_role=api, worker_role=worker)
    op.execute(
        """
        CREATE TABLE reminders (
          id                 uuid PRIMARY KEY,
          user_id            uuid NOT NULL CONSTRAINT fk_reminders_user REFERENCES users (id),
          item_type          text NOT NULL CONSTRAINT ck_reminders_item_type
                             CHECK (item_type IN ('work_item', 'conversation', 'meeting')),
          item_id            uuid NOT NULL,
          person_id          uuid NULL CONSTRAINT fk_reminders_person REFERENCES persons (id),
          reminder_type      text NOT NULL CONSTRAINT ck_reminders_type
                             CHECK (reminder_type IN ('deadline', 'overdue', 'commitment', 'waiting_for',
                                                      'follow_up', 'meeting_prep')),
          slot               text NOT NULL,
          fire_at            timestamptz NOT NULL,
          first_fire_at      timestamptz NOT NULL,
          state              text NOT NULL DEFAULT 'pending' CONSTRAINT ck_reminders_state
                             CHECK (state IN ('pending', 'delivered', 'snoozed', 'dismissed', 'acted',
                                              'suppressed', 'cancelled')),
          fingerprint        bytea NOT NULL,
          material_key       bytea NOT NULL,
          reason             jsonb NOT NULL,
          priority           real NOT NULL DEFAULT 0,
          proactive_eligible boolean NOT NULL,
          proactive          boolean NULL,
          delivery_seq       smallint NOT NULL DEFAULT 0,
          delivered_at       timestamptz NULL,
          snoozed_until      timestamptz NULL,
          snooze_count       smallint NOT NULL DEFAULT 0,
          closed_at          timestamptz NULL,
          closed_reason      text NULL,
          version            int NOT NULL DEFAULT 1,
          created_at         timestamptz NOT NULL DEFAULT now(),
          updated_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_reminders_fp ON reminders (user_id, fingerprint)")
    op.execute("CREATE INDEX ix_reminders_due ON reminders (fire_at) WHERE state IN ('pending', 'snoozed')")
    op.execute("CREATE INDEX ix_reminders_user_state ON reminders (user_id, state, fire_at DESC, id)")
    op.execute("CREATE INDEX ix_reminders_item ON reminders (user_id, item_type, item_id, reminder_type)")
    op.execute(
        """
        CREATE TABLE notifications (
          id          uuid PRIMARY KEY,
          user_id     uuid NOT NULL CONSTRAINT fk_notifications_user REFERENCES users (id),
          reminder_id uuid NOT NULL CONSTRAINT fk_notifications_reminder REFERENCES reminders (id),
          channel     text NOT NULL CONSTRAINT ck_notifications_channel CHECK (channel IN ('in_app', 'web_push')),
          seq         smallint NOT NULL,
          state       text NOT NULL CONSTRAINT ck_notifications_state
                      CHECK (state IN ('pending', 'sending', 'sent', 'failed')),
          attempts    smallint NOT NULL DEFAULT 0,
          last_error  text NULL,
          sent_at     timestamptz NULL,
          read_at     timestamptz NULL,
          created_at  timestamptz NOT NULL DEFAULT now(),
          updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_notifications ON notifications (reminder_id, channel, seq)")
    op.execute(
        "CREATE INDEX ix_notifications_user ON notifications (user_id, created_at DESC, id) WHERE channel = 'in_app'"
    )
    op.execute(
        "CREATE INDEX ix_notifications_pending ON notifications (updated_at) WHERE state IN ('pending', 'sending')"
    )
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER tr_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION eca_set_updated_at()"
        )
    for table in TABLES:
        for stmt in user_isolation_ddl(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in reversed(TABLES):
        for stmt in drop_user_isolation_ddl(table):
            op.execute(stmt)
        op.execute(f"DROP TABLE {table}")
