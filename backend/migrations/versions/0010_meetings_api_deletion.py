"""Slices 1.6-1.9: meetings from calendar, feedback events, idempotency keys, deletion grants.

- ``meetings``, ``meeting_participants`` (slice 1.6; ``meetings`` module): business tables.
- ``feedback_events`` (slice 1.7): every user correction, business table.
- ``idempotency_keys`` (slice 1.7; ``platform``): ``Idempotency-Key`` results for 24 h, business
  table (keyed by user).
- Account deletion (slice 1.9) runs as the worker role per user: the worker may DELETE only its
  own (``app.user_id``) users row, and only once the row is ``deleting``
  (``users_worker_delete``). ``audit_log.user_id`` is nulled before the user row goes.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("meetings", "meeting_participants", "feedback_events", "idempotency_keys")


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
        CREATE TABLE meetings (
          id                  uuid PRIMARY KEY,
          user_id             uuid NOT NULL CONSTRAINT fk_meetings_user REFERENCES users (id),
          source_item_id      uuid NOT NULL CONSTRAINT ux_meetings_source_item UNIQUE
                              CONSTRAINT fk_meetings_source_item REFERENCES source_items (id),
          title               text NULL,
          description         text NULL,
          starts_at           timestamptz NOT NULL,
          ends_at             timestamptz NOT NULL,
          timezone            text NULL,
          series_key          text NULL,
          conference_uri      text NULL,
          organizer_person_id uuid NULL CONSTRAINT fk_meetings_organizer REFERENCES persons (id),
          status              text NOT NULL DEFAULT 'scheduled' CONSTRAINT ck_meetings_status
                              CHECK (status IN ('scheduled', 'occurred', 'cancelled')),
          processing_status   text NOT NULL DEFAULT 'none',
          summary             jsonb NULL,
          prep_brief          jsonb NULL,
          prep_brief_version  int NOT NULL DEFAULT 0,
          project_hint        text NULL,
          version             int NOT NULL DEFAULT 1,
          created_at          timestamptz NOT NULL DEFAULT now(),
          updated_at          timestamptz NOT NULL DEFAULT now(),
          deleted_at          timestamptz NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_meetings_user_time ON meetings (user_id, starts_at) WHERE deleted_at IS NULL")
    op.execute(
        """
        CREATE TABLE meeting_participants (
          meeting_id      uuid NOT NULL CONSTRAINT fk_meeting_participants_meeting REFERENCES meetings (id),
          person_id       uuid NOT NULL CONSTRAINT fk_meeting_participants_person REFERENCES persons (id),
          user_id         uuid NOT NULL CONSTRAINT fk_meeting_participants_user REFERENCES users (id),
          response_status text NULL,
          is_organizer    boolean NOT NULL DEFAULT false,
          attended        boolean NULL,
          origin          text NOT NULL DEFAULT 'source' CONSTRAINT ck_meeting_participants_origin
                          CHECK (origin IN ('source', 'ai', 'user')),
          CONSTRAINT pk_meeting_participants PRIMARY KEY (meeting_id, person_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE feedback_events (
          id          uuid PRIMARY KEY,
          user_id     uuid NOT NULL CONSTRAINT fk_feedback_events_user REFERENCES users (id),
          target_type text NOT NULL,
          target_id   uuid NOT NULL,
          action      text NOT NULL,
          before      jsonb NULL,
          after       jsonb NULL,
          created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_feedback_events_target ON feedback_events (user_id, target_type, target_id)")
    op.execute(
        """
        CREATE TABLE idempotency_keys (
          user_id      uuid NOT NULL CONSTRAINT fk_idempotency_keys_user REFERENCES users (id),
          key          text NOT NULL,
          request_hash bytea NOT NULL,
          status_code  smallint NULL,
          response     jsonb NULL,
          created_at   timestamptz NOT NULL DEFAULT now(),
          expires_at   timestamptz NOT NULL,
          CONSTRAINT pk_idempotency_keys PRIMARY KEY (user_id, key)
        )
        """
    )
    op.execute("CREATE INDEX ix_idempotency_keys_expiry ON idempotency_keys (expires_at)")
    op.execute(
        "CREATE TRIGGER tr_meetings_updated_at BEFORE UPDATE ON meetings "
        "FOR EACH ROW EXECUTE FUNCTION eca_set_updated_at()"
    )
    for table in TABLES:
        for stmt in user_isolation_ddl(table):
            op.execute(stmt)

    # Account deletion (slice 1.9): the worker deletes the user's own row once it is 'deleting'.
    op.execute(f"GRANT DELETE ON users TO {worker}")
    op.execute(
        f"CREATE POLICY users_worker_delete ON users FOR DELETE TO {worker} "
        "USING (id = eca_current_user_id() AND status = 'deleting')"
    )


def downgrade() -> None:
    _, worker = _roles()
    op.execute("DROP POLICY users_worker_delete ON users")
    op.execute(f"REVOKE DELETE ON users FROM {worker}")
    for table in reversed(TABLES):
        for stmt in drop_user_isolation_ddl(table):
            op.execute(stmt)
        op.execute(f"DROP TABLE {table}")
