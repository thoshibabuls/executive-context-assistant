"""Transactional outbox and event consumptions, with their grants and row-level security.

BACKEND_DESIGN.md §7.3 (schema), §7.3.1 (no FK to ``users`` yet: slice 1.1 adds
``fk_outbox_user``), §7.6 (privileges and policies), §7.7 (migration boundaries).

Default privileges (0001, 0002) give both runtime roles SELECT, INSERT, UPDATE and DELETE on new
tables; this revision trims them in the same transaction that creates the tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
        CREATE TABLE outbox (
          id              uuid PRIMARY KEY,
          user_id         uuid NULL,
          event_type      text NOT NULL,
          aggregate_type  text NOT NULL,
          aggregate_id    uuid NOT NULL,
          payload         jsonb NOT NULL,
          correlation     jsonb NOT NULL DEFAULT '{}',
          status          text NOT NULL DEFAULT 'pending'
                          CONSTRAINT ck_outbox_status CHECK (status IN ('pending', 'dispatched', 'failed')),
          attempts        int NOT NULL DEFAULT 0,
          next_attempt_at timestamptz NOT NULL DEFAULT now(),
          last_error      text NULL,
          created_at      timestamptz NOT NULL DEFAULT now(),
          dispatched_at   timestamptz NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_outbox_pending ON outbox (next_attempt_at) WHERE status = 'pending'")
    op.execute("CREATE INDEX ix_outbox_user ON outbox (user_id) WHERE user_id IS NOT NULL")
    op.execute(
        """
        CREATE TABLE event_consumptions (
          event_id    uuid NOT NULL CONSTRAINT fk_event_consumptions_event REFERENCES outbox (id),
          handler     text NOT NULL,
          consumed_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT pk_event_consumptions PRIMARY KEY (event_id, handler)
        )
        """
    )

    # Trim the default privileges to the matrix of BACKEND_DESIGN.md §7.6.
    op.execute(f"REVOKE SELECT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON outbox FROM {api}")
    op.execute(f"REVOKE ALL ON event_consumptions FROM {api}")
    op.execute(f"REVOKE UPDATE, TRUNCATE, REFERENCES, TRIGGER ON event_consumptions FROM {worker}")
    op.execute(f"REVOKE TRUNCATE, REFERENCES, TRIGGER ON outbox FROM {worker}")

    for table in ("outbox", "event_consumptions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY outbox_api_insert ON outbox FOR INSERT TO {api} "
        "WITH CHECK (user_id = eca_current_user_id())"
    )
    op.execute(f"CREATE POLICY outbox_worker_all ON outbox FOR ALL TO {worker} USING (true) WITH CHECK (true)")
    op.execute(
        f"CREATE POLICY event_consumptions_worker_all ON event_consumptions FOR ALL TO {worker} "
        "USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    # Policies, indexes and grants are dropped with the tables.
    op.execute("DROP TABLE event_consumptions")
    op.execute("DROP TABLE outbox")
