"""Slice 2.2: ``retrieval_traces`` (content-free record of every packet assembly).

BACKEND_DESIGN.md §17.5 (DDL), §6.2 (90-day retention); CONTEXT_ARCHITECTURE.md §9.10. A
user-owned business table under ``retrieval_traces_user_isolation``: the API role writes traces
of chat assemblies, the worker role deletes expired ones per user.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0013"
down_revision: str | None = "0012"
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
        CREATE TABLE retrieval_traces (
          id             uuid PRIMARY KEY,
          user_id        uuid NOT NULL CONSTRAINT fk_retrieval_traces_user REFERENCES users (id),
          query_hash     bytea NOT NULL,
          surface        text NOT NULL CONSTRAINT ck_retrieval_traces_surface CHECK (surface IN ('chat', 'api', 'eval')),
          scenario       text NOT NULL,
          planner        text NOT NULL CONSTRAINT ck_retrieval_traces_planner
                         CHECK (planner IN ('rules', 'ai', 'fallback', 'fixed')),
          plan           jsonb NOT NULL,
          candidates     jsonb NOT NULL,
          selected       jsonb NOT NULL,
          coverage       jsonb NOT NULL,
          context_tokens int NOT NULL,
          budget_tokens  int NULL,
          latency_ms     int NOT NULL,
          created_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_retrieval_traces_user_time ON retrieval_traces (user_id, created_at DESC)")
    for stmt in user_isolation_ddl("retrieval_traces"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_user_isolation_ddl("retrieval_traces"):
        op.execute(stmt)
    op.execute("DROP TABLE retrieval_traces")
