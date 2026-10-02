"""Slice 2.4: ``chat_sessions`` and ``chat_messages`` (``chat`` module).

BACKEND_DESIGN.md §17.5 (DDL), §6.2 (questions USER-AUTHORED, answers AI-DERIVED with citation
snapshots; the user deletes sessions permanently); CONTEXT_ARCHITECTURE.md §13 (focus map).
``retrieval_trace_id`` has no FK: traces expire after 90 days. Business tables under per-user RLS.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("chat_sessions", "chat_messages")


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
        CREATE TABLE chat_sessions (
          id               uuid PRIMARY KEY,
          user_id          uuid NOT NULL CONSTRAINT fk_chat_sessions_user REFERENCES users (id),
          title            text NULL,
          scope            jsonb NOT NULL DEFAULT '{"kind": "global"}',
          session_entities jsonb NOT NULL DEFAULT '[]',
          version          int NOT NULL DEFAULT 1,
          created_at       timestamptz NOT NULL DEFAULT now(),
          last_active_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_chat_sessions_user ON chat_sessions (user_id, last_active_at DESC, id)")
    op.execute(
        """
        CREATE TABLE chat_messages (
          id                 uuid PRIMARY KEY,
          session_id         uuid NOT NULL CONSTRAINT fk_chat_messages_session REFERENCES chat_sessions (id),
          user_id            uuid NOT NULL CONSTRAINT fk_chat_messages_user REFERENCES users (id),
          role               text NOT NULL CONSTRAINT ck_chat_messages_role CHECK (role IN ('user', 'assistant')),
          content            text NOT NULL,
          reply_to_id        uuid NULL CONSTRAINT fk_chat_messages_reply_to REFERENCES chat_messages (id),
          scenario           text NULL,
          answer_tier        text NULL CONSTRAINT ck_chat_messages_tier
                             CHECK (answer_tier IN ('deterministic', 'T1', 'T2', 'abstain', 'degraded')),
          claims             jsonb NOT NULL DEFAULT '[]',
          citations          jsonb NOT NULL DEFAULT '[]',
          confidence         text NULL CONSTRAINT ck_chat_messages_confidence
                             CHECK (confidence IN ('low', 'medium', 'high')),
          provenance         jsonb NULL,
          retrieval_trace_id uuid NULL,
          ai_call_ids        uuid[] NOT NULL DEFAULT '{}',
          created_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_chat_messages_session ON chat_messages (session_id, created_at, id)")
    for table in TABLES:
        for stmt in user_isolation_ddl(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in reversed(TABLES):
        for stmt in drop_user_isolation_ddl(table):
            op.execute(stmt)
        op.execute(f"DROP TABLE {table}")
