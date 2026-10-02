"""Slice 2.1: ``chunks``, the hybrid search index (vector + full text) owned by ``retrieval``.

BACKEND_DESIGN.md §17.2 and §17.5 (DDL and indexes), CONTEXT_ARCHITECTURE.md §9.9-§9.10
(chunk units, FTS configuration, embedding model per row). A user-owned business table under
``chunks_user_isolation`` with the default DML grants of both runtime roles (§7.6). The table is
new and empty, so its indexes are created in the revision transaction.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0011"
down_revision: str | None = "0010"
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
        CREATE TABLE chunks (
          id              uuid PRIMARY KEY,
          user_id         uuid NOT NULL CONSTRAINT fk_chunks_user REFERENCES users (id),
          source_item_id  uuid NOT NULL CONSTRAINT fk_chunks_source_item REFERENCES source_items (id),
          chunk_index     int NOT NULL,
          kind            text NOT NULL CONSTRAINT ck_chunks_kind
                          CHECK (kind IN ('email', 'calendar_event', 'transcript', 'summary')),
          title           text NULL,
          text            text NOT NULL,
          token_count     int NOT NULL,
          content_hash    bytea NOT NULL,
          occurred_at     timestamptz NOT NULL,
          conversation_id uuid NULL CONSTRAINT fk_chunks_conversation REFERENCES conversations (id),
          meeting_id      uuid NULL CONSTRAINT fk_chunks_meeting REFERENCES meetings (id),
          person_ids      uuid[] NOT NULL DEFAULT '{}',
          embedding       halfvec(768) NULL,
          embedding_model text NULL,
          embedded_at     timestamptz NULL,
          tsv             tsvector GENERATED ALWAYS AS (
                            setweight(to_tsvector('english', coalesce(title, '')), 'A')
                            || setweight(to_tsvector('english', text), 'B')) STORED,
          created_at      timestamptz NOT NULL DEFAULT now(),
          updated_at      timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ck_chunks_embedding_model CHECK ((embedding IS NULL) = (embedding_model IS NULL))
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_chunks ON chunks (source_item_id, chunk_index)")
    op.execute("CREATE INDEX ix_chunks_hnsw ON chunks USING hnsw (embedding halfvec_cosine_ops)")
    op.execute("CREATE INDEX ix_chunks_fts ON chunks USING gin (tsv)")
    op.execute("CREATE INDEX ix_chunks_user_time ON chunks (user_id, occurred_at DESC)")
    op.execute("CREATE INDEX ix_chunks_persons ON chunks USING gin (person_ids)")
    op.execute(
        "CREATE INDEX ix_chunks_conversation ON chunks (user_id, conversation_id) WHERE conversation_id IS NOT NULL"
    )
    op.execute(
        "CREATE TRIGGER tr_chunks_updated_at BEFORE UPDATE ON chunks "
        "FOR EACH ROW EXECUTE FUNCTION eca_set_updated_at()"
    )
    for stmt in user_isolation_ddl("chunks"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_user_isolation_ddl("chunks"):
        op.execute(stmt)
    op.execute("DROP TABLE chunks")
