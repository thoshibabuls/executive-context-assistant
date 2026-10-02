"""Slice 2.5: ``projects`` and ``project_members`` (``projects`` module); expand
``work_items.project_id`` with its FK (deferred since Batch A, BACKEND_DESIGN.md §17.2).

BACKEND_DESIGN.md §17.5 (DDL), CONTEXT_ARCHITECTURE.md §10.3, §11.8, §12.5 (suggested after ≥ 3
sources share a hint; confirmed by the user). Expand only: the new column is nullable and no
existing row is rewritten. Its index is created ``CONCURRENTLY`` by 0016. Business tables under
per-user RLS.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("projects", "project_members")


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
        CREATE TABLE projects (
          id                  uuid PRIMARY KEY,
          user_id             uuid NOT NULL CONSTRAINT fk_projects_user REFERENCES users (id),
          name                text NOT NULL,
          description         text NULL,
          aliases             text[] NOT NULL DEFAULT '{}',
          hint_key            text NULL,
          status              text NOT NULL DEFAULT 'active' CONSTRAINT ck_projects_status
                              CHECK (status IN ('active', 'archived')),
          importance_user     smallint NULL,
          origin              text NOT NULL CONSTRAINT ck_projects_origin CHECK (origin IN ('ai', 'user')),
          verification_status text NOT NULL CONSTRAINT ck_projects_verification
                              CHECK (verification_status IN ('suggested', 'confirmed', 'rejected', 'user_created')),
          suggestion_sources  int NULL,
          user_fields         text[] NOT NULL DEFAULT '{}',
          version             int NOT NULL DEFAULT 1,
          created_at          timestamptz NOT NULL DEFAULT now(),
          updated_at          timestamptz NOT NULL DEFAULT now(),
          deleted_at          timestamptz NULL
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_projects_hint ON projects (user_id, hint_key) WHERE hint_key IS NOT NULL")
    op.execute(
        "CREATE INDEX ix_projects_name_trgm ON projects USING gin (lower(name) gin_trgm_ops) WHERE deleted_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE project_members (
          project_id uuid NOT NULL CONSTRAINT fk_project_members_project REFERENCES projects (id),
          person_id  uuid NOT NULL CONSTRAINT fk_project_members_person REFERENCES persons (id),
          user_id    uuid NOT NULL CONSTRAINT fk_project_members_user REFERENCES users (id),
          role       text NULL,
          origin     text NOT NULL CONSTRAINT ck_project_members_origin CHECK (origin IN ('computed', 'user')),
          CONSTRAINT pk_project_members PRIMARY KEY (project_id, person_id)
        )
        """
    )
    op.execute(
        "CREATE TRIGGER tr_projects_updated_at BEFORE UPDATE ON projects "
        "FOR EACH ROW EXECUTE FUNCTION eca_set_updated_at()"
    )
    op.execute(
        "ALTER TABLE work_items ADD COLUMN project_id uuid NULL CONSTRAINT fk_wi_project REFERENCES projects (id)"
    )
    for table in TABLES:
        for stmt in user_isolation_ddl(table):
            op.execute(stmt)


def downgrade() -> None:
    op.execute("ALTER TABLE work_items DROP COLUMN project_id")
    for table in reversed(TABLES):
        for stmt in drop_user_isolation_ddl(table):
            op.execute(stmt)
        op.execute(f"DROP TABLE {table}")
