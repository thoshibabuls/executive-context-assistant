"""AI telemetry: ``ai_calls`` meter and ``ai_cost_rollups``, with grants and row-level security.

BACKEND_DESIGN.md §5.5 (metering and roll-ups), §6.2 (content-free telemetry), §7.6 (privileges
and policies; no FK to ``users`` yet: slice 1.1 adds ``fk_ai_calls_user`` and
``fk_ai_cost_rollups_user``), §7.7 (migration boundaries).

The API role may only INSERT meter rows for the current user and read its own roll-ups; the
worker role writes meter rows for background calls and computes roll-ups for every user.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0005"
down_revision: str | None = "0004"
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
        CREATE TABLE ai_calls (
          id                  uuid PRIMARY KEY,
          user_id             uuid NULL,
          role                text NOT NULL,
          inventory_id        text NOT NULL,
          model               text NOT NULL,
          prompt_version      text NULL,
          schema_version      text NULL,
          input_tokens        int NOT NULL DEFAULT 0 CONSTRAINT ck_ai_calls_input CHECK (input_tokens >= 0),
          cached_input_tokens int NOT NULL DEFAULT 0 CONSTRAINT ck_ai_calls_cached CHECK (cached_input_tokens >= 0),
          output_tokens       int NOT NULL DEFAULT 0 CONSTRAINT ck_ai_calls_output CHECK (output_tokens >= 0),
          thinking_tokens     int NOT NULL DEFAULT 0 CONSTRAINT ck_ai_calls_thinking CHECK (thinking_tokens >= 0),
          audio_seconds       numeric(10, 3) NOT NULL DEFAULT 0,
          latency_ms          int NOT NULL DEFAULT 0,
          est_cost_usd        numeric(14, 8) NOT NULL DEFAULT 0
                              CONSTRAINT ck_ai_calls_cost CHECK (est_cost_usd >= 0),
          status              text NOT NULL CONSTRAINT ck_ai_calls_status CHECK (status IN (
                                'ok', 'schema_invalid', 'safety_block', 'empty_response', 'timeout',
                                'rate_limited', 'provider_error')),
          error_code          text NULL,
          attempt             smallint NOT NULL DEFAULT 1 CONSTRAINT ck_ai_calls_attempt CHECK (attempt >= 1),
          is_fallback         boolean NOT NULL DEFAULT false,
          created_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_ai_calls_created ON ai_calls (created_at)")
    op.execute("CREATE INDEX ix_ai_calls_user_created ON ai_calls (user_id, created_at) WHERE user_id IS NOT NULL")
    op.execute(
        """
        CREATE TABLE ai_cost_rollups (
          bucket_start        timestamptz NOT NULL,
          user_id             uuid NULL,
          role                text NOT NULL,
          model               text NOT NULL,
          calls               bigint NOT NULL,
          failed_calls        bigint NOT NULL,
          retry_calls         bigint NOT NULL,
          fallback_calls      bigint NOT NULL,
          input_tokens        bigint NOT NULL,
          cached_input_tokens bigint NOT NULL,
          output_tokens       bigint NOT NULL,
          thinking_tokens     bigint NOT NULL,
          audio_seconds       numeric(12, 3) NOT NULL,
          latency_ms_total    bigint NOT NULL,
          est_cost_usd        numeric(16, 8) NOT NULL,
          computed_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_ai_cost_rollups_bucket ON ai_cost_rollups "
        "(bucket_start, user_id, role, model) NULLS NOT DISTINCT"
    )
    op.execute("CREATE INDEX ix_ai_cost_rollups_user ON ai_cost_rollups (user_id, bucket_start)")

    # Trim the default privileges to the matrix of BACKEND_DESIGN.md §7.6.
    op.execute(f"REVOKE SELECT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ai_calls FROM {api}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ai_cost_rollups FROM {api}")
    op.execute(f"REVOKE TRUNCATE, REFERENCES, TRIGGER ON ai_calls FROM {worker}")
    op.execute(f"REVOKE TRUNCATE, REFERENCES, TRIGGER ON ai_cost_rollups FROM {worker}")

    for table in ("ai_calls", "ai_cost_rollups"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY ai_calls_api_insert ON ai_calls FOR INSERT TO {api} "
        "WITH CHECK (user_id = eca_current_user_id())"
    )
    op.execute(f"CREATE POLICY ai_calls_worker_all ON ai_calls FOR ALL TO {worker} USING (true) WITH CHECK (true)")
    op.execute(
        f"CREATE POLICY ai_cost_rollups_api_read ON ai_cost_rollups FOR SELECT TO {api} "
        "USING (user_id = eca_current_user_id())"
    )
    op.execute(
        f"CREATE POLICY ai_cost_rollups_worker_all ON ai_cost_rollups FOR ALL TO {worker} "
        "USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    # Policies, indexes and grants are dropped with the tables.
    op.execute("DROP TABLE ai_cost_rollups")
    op.execute("DROP TABLE ai_calls")
