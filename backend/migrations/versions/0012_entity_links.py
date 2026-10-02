"""Slice 2.1: ``entity_links`` owned by ``work`` (thread continuation, later duplicates and relations).

BACKEND_DESIGN.md §17.5 (DDL), CONTEXT_ARCHITECTURE.md §12.2 and §12.7 (``continues`` links).
Endpoints are polymorphic (type + ID) and validated by the ``work`` service, like
``context_events.entity_id`` (§17.1). A user-owned business table under
``entity_links_user_isolation``.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TYPES = "('work_item', 'decision', 'conversation', 'meeting', 'project')"


def _roles() -> tuple[str, str]:
    api = context.config.attributes.get("runtime_role", "eca_app")
    worker = context.config.attributes.get("worker_role", "eca_worker")
    assert isinstance(api, str) and isinstance(worker, str)
    return validate_identifier(api), validate_identifier(worker)


def upgrade() -> None:
    api, worker = _roles()
    check_runtime_roles(op.get_bind(), api_role=api, worker_role=worker)

    op.execute(
        f"""
        CREATE TABLE entity_links (
          id                  uuid PRIMARY KEY,
          user_id             uuid NOT NULL CONSTRAINT fk_entity_links_user REFERENCES users (id),
          from_type           text NOT NULL CONSTRAINT ck_entity_links_from_type CHECK (from_type IN {_TYPES}),
          from_id             uuid NOT NULL,
          to_type             text NOT NULL CONSTRAINT ck_entity_links_to_type CHECK (to_type IN {_TYPES}),
          to_id               uuid NOT NULL,
          relation            text NOT NULL CONSTRAINT ck_entity_links_relation
                              CHECK (relation IN ('continues', 'possible_duplicate', 'relates_to')),
          confidence          real NOT NULL CONSTRAINT ck_entity_links_confidence CHECK (confidence BETWEEN 0 AND 1),
          method              text NOT NULL CONSTRAINT ck_entity_links_method
                              CHECK (method IN ('deterministic', 'embedding_match', 'llm', 'user')),
          origin              text NOT NULL CONSTRAINT ck_entity_links_origin CHECK (origin IN ('computed', 'ai', 'user')),
          verification_status text NOT NULL DEFAULT 'suggested' CONSTRAINT ck_entity_links_verification
                              CHECK (verification_status IN ('suggested', 'confirmed', 'rejected')),
          scores              jsonb NOT NULL DEFAULT '{{}}',
          created_at          timestamptz NOT NULL DEFAULT now(),
          updated_at          timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ux_entity_links UNIQUE (user_id, from_type, from_id, to_type, to_id, relation),
          CONSTRAINT ck_entity_links_not_self CHECK (NOT (from_type = to_type AND from_id = to_id))
        )
        """
    )
    op.execute("CREATE INDEX ix_entity_links_to ON entity_links (user_id, to_type, to_id, relation)")
    op.execute(
        "CREATE TRIGGER tr_entity_links_updated_at BEFORE UPDATE ON entity_links "
        "FOR EACH ROW EXECUTE FUNCTION eca_set_updated_at()"
    )
    for stmt in user_isolation_ddl("entity_links"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_user_isolation_ddl("entity_links"):
        op.execute(stmt)
    op.execute("DROP TABLE entity_links")
