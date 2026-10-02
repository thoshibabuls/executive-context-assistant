"""Slices 1.1 and 1.2 flows: sign-in state, sessions, audit log, deletion requests.

BACKEND_DESIGN.md §7.6 (access model), §10.2 (``state`` single use), §16.5 (auth endpoints);
TECHNICAL_DESIGN.md §9.1 (``auth_sessions``, ``audit_log``, ``deletion_jobs``).

Sign-in and session resolution happen before ``app.user_id`` is known, and every user table is
under per-user RLS. Two ``SECURITY DEFINER`` functions, executable only by the API role, return
exactly one user ID for a verified Google identity or a session token hash; they read no other
column and allow no write. They are the documented exceptions to "no cross-user read by the API"
(§7.6, Batch B).

- ``oauth_states``: short-lived, single-use state rows (PKCE verifier, nonce) for sign-in and
  connect. Not user content; API role only (``oauth_states_api_all``), no worker access.
- ``auth_sessions``, ``deletion_jobs``: user-owned business tables (``_user_isolation``).
- ``audit_log``: owned by ``privacy`` through ``platform.audit``; API INSERT only (own user or
  NULL for pre-sign-in failures), worker SELECT/INSERT/DELETE (retention).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0009"
down_revision: str | None = "0008"
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

    op.execute("ALTER TABLE users ADD COLUMN google_sub text NULL CONSTRAINT ux_users_google_sub UNIQUE")
    op.execute(
        """
        CREATE TABLE oauth_states (
          state_hash    bytea PRIMARY KEY,
          purpose       text NOT NULL CONSTRAINT ck_oauth_states_purpose CHECK (purpose IN ('signin', 'connect')),
          code_verifier text NOT NULL,
          nonce         text NOT NULL,
          user_id       uuid NULL CONSTRAINT fk_oauth_states_user REFERENCES users (id),
          scopes        text[] NOT NULL DEFAULT '{}',
          redirect_to   text NULL,
          created_at    timestamptz NOT NULL DEFAULT now(),
          expires_at    timestamptz NOT NULL,
          consumed_at   timestamptz NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE auth_sessions (
          id           uuid PRIMARY KEY,
          user_id      uuid NOT NULL CONSTRAINT fk_auth_sessions_user REFERENCES users (id),
          session_hash bytea NOT NULL CONSTRAINT ux_auth_sessions_hash UNIQUE,
          csrf_hash    bytea NOT NULL,
          created_at   timestamptz NOT NULL DEFAULT now(),
          expires_at   timestamptz NOT NULL,
          last_seen_at timestamptz NOT NULL DEFAULT now(),
          reauth_at    timestamptz NOT NULL DEFAULT now(),
          ip           text NULL,
          user_agent   text NULL,
          revoked_at   timestamptz NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_auth_sessions_user ON auth_sessions (user_id) WHERE revoked_at IS NULL")
    op.execute(
        """
        CREATE TABLE audit_log (
          id          uuid PRIMARY KEY,
          user_id     uuid NULL CONSTRAINT fk_audit_log_user REFERENCES users (id),
          actor       text NOT NULL,
          action      text NOT NULL,
          target_type text NULL,
          target_id   uuid NULL,
          ip          text NULL,
          metadata    jsonb NOT NULL DEFAULT '{}',
          created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_audit_log_user_time ON audit_log (user_id, created_at DESC)")
    op.execute(
        """
        CREATE TABLE deletion_jobs (
          id           uuid PRIMARY KEY,
          user_id      uuid NOT NULL CONSTRAINT fk_deletion_jobs_user REFERENCES users (id),
          kind         text NOT NULL DEFAULT 'account' CONSTRAINT ck_deletion_jobs_kind
                       CHECK (kind IN ('account', 'source_purge')),
          status       text NOT NULL DEFAULT 'pending' CONSTRAINT ck_deletion_jobs_status
                       CHECK (status IN ('pending', 'running', 'done', 'failed')),
          progress     jsonb NOT NULL DEFAULT '{}',
          requested_at timestamptz NOT NULL DEFAULT now(),
          updated_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_deletion_jobs_active ON deletion_jobs (user_id, kind) "
        "WHERE status IN ('pending', 'running')"
    )

    # oauth_states: API only.
    op.execute(f"REVOKE ALL ON oauth_states FROM {worker}")
    op.execute(f"REVOKE TRUNCATE, REFERENCES, TRIGGER ON oauth_states FROM {api}")
    op.execute("ALTER TABLE oauth_states ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE oauth_states FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY oauth_states_api_all ON oauth_states FOR ALL TO {api} USING (true) WITH CHECK (true)")

    # audit_log: API insert-only; worker reads and purges.
    op.execute(f"REVOKE SELECT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON audit_log FROM {api}")
    op.execute(f"REVOKE UPDATE, TRUNCATE, REFERENCES, TRIGGER ON audit_log FROM {worker}")
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY audit_log_api_insert ON audit_log FOR INSERT TO {api} "
        "WITH CHECK (user_id IS NULL OR user_id = eca_current_user_id())"
    )
    op.execute(f"CREATE POLICY audit_log_worker_all ON audit_log FOR ALL TO {worker} USING (true) WITH CHECK (true)")

    for table in ("auth_sessions", "deletion_jobs"):
        for stmt in user_isolation_ddl(table):
            op.execute(stmt)

    op.execute(
        """
        CREATE FUNCTION eca_signin_user_id(p_sub text, p_email citext) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$ SELECT id FROM users WHERE google_sub = p_sub
              UNION ALL
              SELECT id FROM users WHERE google_sub IS NULL AND email = p_email
              LIMIT 1 $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION eca_session_user_id(p_hash bytea) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$ SELECT user_id FROM auth_sessions
               WHERE session_hash = p_hash AND revoked_at IS NULL AND expires_at > now() $$
        """
    )
    for fn in ("eca_signin_user_id(text, citext)", "eca_session_user_id(bytea)"):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {api}")


def downgrade() -> None:
    op.execute("DROP FUNCTION eca_session_user_id(bytea)")
    op.execute("DROP FUNCTION eca_signin_user_id(text, citext)")
    for table in ("deletion_jobs", "auth_sessions"):
        for stmt in drop_user_isolation_ddl(table):
            op.execute(stmt)
    for table in ("deletion_jobs", "audit_log", "auth_sessions", "oauth_states"):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE users DROP COLUMN google_sub")
