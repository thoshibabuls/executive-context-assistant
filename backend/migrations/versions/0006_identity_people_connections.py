"""Batch A data model of slices 1.1 and 1.2: users, persons, identifiers, organizations,
connections and sync cursors; the deferred FKs to ``users``; the worker's users enumeration.

BACKEND_DESIGN.md §7.3.1 and §7.6 (FKs and access model), §11.1 (cursors), §17 (conventions);
TECHNICAL_DESIGN.md §9.1 (columns); IMPLEMENTATION_PLAN.md "Phase 1 batches". Flows (sign-in,
sessions, Google connect, tokens) are Batch B; the token columns exist but stay NULL.

Every table is a user-owned business table under ``<table>_user_isolation`` for all roles. The
only exception is ``users``: the worker role gets column SELECT on (id, status, timezone,
work_hours) and the policy ``users_worker_enumerate`` (§7.6), and nothing else on that table.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER_TABLES = ("organizations", "persons", "person_identifiers", "connections", "sync_cursors")
UPDATED_AT_TABLES = ("users", "organizations", "persons", "connections")

SET_UPDATED_AT_SQL = """
CREATE OR REPLACE FUNCTION eca_set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$
"""


def _roles() -> tuple[str, str]:
    api = context.config.attributes.get("runtime_role", "eca_app")
    worker = context.config.attributes.get("worker_role", "eca_worker")
    assert isinstance(api, str) and isinstance(worker, str)
    return validate_identifier(api), validate_identifier(worker)


def upgrade() -> None:
    api, worker = _roles()
    check_runtime_roles(op.get_bind(), api_role=api, worker_role=worker)

    op.execute(SET_UPDATED_AT_SQL)
    op.execute("REVOKE ALL ON FUNCTION eca_set_updated_at() FROM PUBLIC")

    op.execute(
        """
        CREATE TABLE users (
          id           uuid PRIMARY KEY,
          email        citext NOT NULL CONSTRAINT ux_users_email UNIQUE,
          display_name text NOT NULL,
          timezone     text NOT NULL DEFAULT 'UTC',
          work_hours   jsonb NOT NULL DEFAULT '{}',
          status       text NOT NULL DEFAULT 'active' CONSTRAINT ck_users_status CHECK (status IN ('active', 'deleting')),
          created_at   timestamptz NOT NULL DEFAULT now(),
          updated_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE organizations (
          id              uuid PRIMARY KEY,
          user_id         uuid NOT NULL CONSTRAINT fk_organizations_user REFERENCES users (id),
          name            text NOT NULL,
          domain          citext NULL,
          importance_user smallint NULL,
          origin          text NOT NULL DEFAULT 'computed' CONSTRAINT ck_organizations_origin CHECK (origin IN ('computed', 'user')),
          created_at      timestamptz NOT NULL DEFAULT now(),
          updated_at      timestamptz NOT NULL DEFAULT now(),
          deleted_at      timestamptz NULL
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_organizations_domain ON organizations (user_id, domain) WHERE domain IS NOT NULL")
    op.execute(
        """
        CREATE TABLE persons (
          id                  uuid PRIMARY KEY,
          user_id             uuid NOT NULL CONSTRAINT fk_persons_user REFERENCES users (id),
          display_name        text NULL,
          primary_email       citext NULL,
          organization_id     uuid NULL CONSTRAINT fk_persons_organization REFERENCES organizations (id),
          role_title          text NULL,
          role_origin         text NULL CONSTRAINT ck_persons_role_origin CHECK (role_origin IN ('signature', 'model', 'user')),
          relationship_type   text NULL,
          importance_user     smallint NULL,
          importance_inferred real NULL,
          is_self             boolean NOT NULL DEFAULT false,
          first_seen_at       timestamptz NULL,
          last_interaction_at timestamptz NULL,
          last_inbound_at     timestamptz NULL,
          last_outbound_at    timestamptz NULL,
          interaction_stats   jsonb NOT NULL DEFAULT '{}',
          merged_into_id      uuid NULL CONSTRAINT fk_persons_merged_into REFERENCES persons (id),
          version             int NOT NULL DEFAULT 1,
          created_at          timestamptz NOT NULL DEFAULT now(),
          updated_at          timestamptz NOT NULL DEFAULT now(),
          deleted_at          timestamptz NULL
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_persons_self ON persons (user_id) WHERE is_self")
    op.execute("CREATE INDEX ix_persons_user_email ON persons (user_id, primary_email) WHERE deleted_at IS NULL")
    op.execute(
        """
        CREATE TABLE person_identifiers (
          id               uuid PRIMARY KEY,
          user_id          uuid NOT NULL CONSTRAINT fk_person_identifiers_user REFERENCES users (id),
          person_id        uuid NOT NULL CONSTRAINT fk_person_identifiers_person REFERENCES persons (id),
          kind             text NOT NULL CONSTRAINT ck_person_identifiers_kind
                           CHECK (kind IN ('email', 'name_alias', 'speaker_label', 'provider_id')),
          value_normalized text NOT NULL,
          source           text NOT NULL CONSTRAINT ck_person_identifiers_source
                           CHECK (source IN ('header', 'signature', 'user', 'self')),
          confidence       real NOT NULL DEFAULT 1.0,
          confirmed        boolean NOT NULL DEFAULT false,
          created_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_person_identifiers ON person_identifiers (user_id, kind, value_normalized) "
        "WHERE kind <> 'name_alias'"
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_person_identifiers_alias ON person_identifiers "
        "(user_id, person_id, value_normalized) WHERE kind = 'name_alias'"
    )
    op.execute(
        "CREATE INDEX ix_person_alias_trgm ON person_identifiers USING gin (value_normalized gin_trgm_ops) "
        "WHERE kind = 'name_alias'"
    )
    op.execute(
        """
        CREATE TABLE connections (
          id                       uuid PRIMARY KEY,
          user_id                  uuid NOT NULL CONSTRAINT fk_connections_user REFERENCES users (id),
          provider                 text NOT NULL CONSTRAINT ck_connections_provider
                                   CHECK (provider IN ('google', 'microsoft', 'slack', 'fake')),
          account_email            citext NOT NULL,
          granted_scopes           text[] NOT NULL DEFAULT '{}',
          refresh_token_ciphertext bytea NULL,
          token_key_version        int NULL,
          status                   text NOT NULL DEFAULT 'active' CONSTRAINT ck_connections_status
                                   CHECK (status IN ('active', 'paused', 'needs_reauth', 'revoked', 'error')),
          last_error               text NULL,
          created_at               timestamptz NOT NULL DEFAULT now(),
          updated_at               timestamptz NOT NULL DEFAULT now(),
          revoked_at               timestamptz NULL,
          CONSTRAINT ux_connections_account UNIQUE (user_id, provider, account_email)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE sync_cursors (
          connection_id        uuid NOT NULL CONSTRAINT fk_sync_cursors_connection REFERENCES connections (id),
          resource             text NOT NULL,
          user_id              uuid NOT NULL CONSTRAINT fk_sync_cursors_user REFERENCES users (id),
          cursor               text NULL,
          cursor_obtained_at   timestamptz NULL,
          query_fingerprint    text NULL,
          import_state         text NOT NULL DEFAULT 'none' CONSTRAINT ck_sync_cursors_import_state
                               CHECK (import_state IN ('none', 'running', 'done')),
          import_page_token    text NULL,
          import_until         timestamptz NULL,
          import_processed     int NOT NULL DEFAULT 0,
          last_attempt_at      timestamptz NULL,
          last_success_at      timestamptz NULL,
          consecutive_failures int NOT NULL DEFAULT 0,
          status               text NOT NULL DEFAULT 'active' CONSTRAINT ck_sync_cursors_status
                               CHECK (status IN ('active', 'paused', 'needs_reauth', 'error')),
          lease_owner          text NULL,
          lease_expires_at     timestamptz NULL,
          CONSTRAINT pk_sync_cursors PRIMARY KEY (connection_id, resource)
        )
        """
    )

    for table in UPDATED_AT_TABLES:
        op.execute(
            f"CREATE TRIGGER tr_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION eca_set_updated_at()"
        )

    # Deferred FKs to users (§7.3.1, §7.6).
    op.execute("ALTER TABLE outbox ADD CONSTRAINT fk_outbox_user FOREIGN KEY (user_id) REFERENCES users (id)")
    op.execute("ALTER TABLE ai_calls ADD CONSTRAINT fk_ai_calls_user FOREIGN KEY (user_id) REFERENCES users (id)")
    op.execute(
        "ALTER TABLE ai_cost_rollups ADD CONSTRAINT fk_ai_cost_rollups_user FOREIGN KEY (user_id) REFERENCES users (id)"
    )

    # users: per-user isolation keyed by id, plus the worker's column-limited enumeration.
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY users_user_isolation ON users "
        "USING (id = eca_current_user_id()) WITH CHECK (id = eca_current_user_id())"
    )
    op.execute(f"CREATE POLICY users_worker_enumerate ON users FOR SELECT TO {worker} USING (true)")
    op.execute(f"REVOKE ALL ON users FROM {worker}")
    op.execute(f"GRANT SELECT (id, status, timezone, work_hours) ON users TO {worker}")

    for table in USER_TABLES:
        for stmt in user_isolation_ddl(table):
            op.execute(stmt)


def downgrade() -> None:
    op.execute("ALTER TABLE ai_cost_rollups DROP CONSTRAINT fk_ai_cost_rollups_user")
    op.execute("ALTER TABLE ai_calls DROP CONSTRAINT fk_ai_calls_user")
    op.execute("ALTER TABLE outbox DROP CONSTRAINT fk_outbox_user")
    for table in reversed(USER_TABLES):
        for stmt in drop_user_isolation_ddl(table):
            op.execute(stmt)
    # Policies, triggers, indexes and grants are dropped with the tables.
    for table in ("sync_cursors", "connections", "person_identifiers", "persons", "organizations", "users"):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION eca_set_updated_at()")
