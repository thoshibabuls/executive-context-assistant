"""Slice 1.4: extractions, work items, decisions, evidence, item evidence and context events.

BACKEND_DESIGN.md §8.1 (extractions), §10.1 (dedupe keys), §17.1 (provenance columns), §17.2
(DDL) and the Batch A schema decisions in §17 (no ``project_id`` FK yet; ``dedupe_embedding``
NULL until item embeddings). Every table is a user-owned business table under
``<table>_user_isolation``.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("extractions", "evidence", "work_items", "decisions", "item_evidence", "context_events")


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
        CREATE TABLE extractions (
          id                   uuid PRIMARY KEY,
          user_id              uuid NOT NULL CONSTRAINT fk_extractions_user REFERENCES users (id),
          source_item_id       uuid NOT NULL CONSTRAINT fk_extractions_source_item REFERENCES source_items (id),
          content_hash         bytea NOT NULL,
          pipeline             text NOT NULL CONSTRAINT ck_extractions_pipeline
                               CHECK (pipeline IN ('email_extract', 'meeting_extract', 'adjudicate')),
          prompt_version       text NOT NULL,
          schema_version       text NOT NULL,
          model                text NULL,
          input_hash           bytea NOT NULL,
          candidate_map        jsonb NOT NULL DEFAULT '{}',
          parent_extraction_id uuid NULL CONSTRAINT fk_extractions_parent REFERENCES extractions (id),
          status               text NOT NULL CONSTRAINT ck_extractions_status CHECK (status IN
                               ('running', 'succeeded', 'failed_retryable', 'failed_permanent', 'superseded')),
          attempts             smallint NOT NULL DEFAULT 0,
          output               jsonb NULL,
          error_code           text NULL,
          apply_status         text NOT NULL DEFAULT 'not_ready' CONSTRAINT ck_extractions_apply_status
                               CHECK (apply_status IN ('not_ready', 'pending', 'applied', 'apply_failed', 'skipped')),
          applied_at           timestamptz NULL,
          created_at           timestamptz NOT NULL DEFAULT now(),
          updated_at           timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_extractions_key ON extractions "
        "(source_item_id, content_hash, pipeline, prompt_version, parent_extraction_id) NULLS NOT DISTINCT"
    )
    op.execute("CREATE INDEX ix_extractions_apply ON extractions (user_id, apply_status) WHERE apply_status = 'pending'")
    op.execute(
        """
        CREATE TABLE evidence (
          id              uuid PRIMARY KEY,
          user_id         uuid NOT NULL CONSTRAINT fk_evidence_user REFERENCES users (id),
          source_item_id  uuid NOT NULL CONSTRAINT fk_evidence_source_item REFERENCES source_items (id),
          extraction_id   uuid NULL CONSTRAINT fk_evidence_extraction REFERENCES extractions (id),
          candidate_index int NULL,
          quote           text NOT NULL,
          char_start      int NULL,
          char_end        int NULL,
          start_ms        int NULL,
          end_ms          int NULL,
          occurred_at     timestamptz NOT NULL,
          created_at      timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ux_evidence_candidate UNIQUE (extraction_id, candidate_index)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE work_items (
          id                          uuid PRIMARY KEY,
          user_id                     uuid NOT NULL CONSTRAINT fk_work_items_user REFERENCES users (id),
          type                        text NOT NULL CONSTRAINT ck_wi_type
                                      CHECK (type IN ('task', 'commitment', 'request', 'follow_up', 'deadline')),
          title                       text NOT NULL,
          description                 text NULL,
          owner_person_id             uuid NULL CONSTRAINT fk_wi_owner REFERENCES persons (id),
          counterparty_person_id      uuid NULL CONSTRAINT fk_wi_counterparty REFERENCES persons (id),
          requester_person_id         uuid NULL CONSTRAINT fk_wi_requester REFERENCES persons (id),
          project_hint                text NULL,
          due_at                      timestamptz NULL,
          due_precision               text NULL CONSTRAINT ck_wi_due_precision
                                      CHECK (due_precision IN ('datetime', 'day', 'week', 'fuzzy')),
          due_text                    text NULL,
          due_kind                    text NULL,
          lifecycle_status            text NOT NULL DEFAULT 'open' CONSTRAINT ck_wi_lifecycle
                                      CHECK (lifecycle_status IN ('open', 'in_progress', 'done', 'cancelled')),
          verification_status         text NOT NULL DEFAULT 'suggested' CONSTRAINT ck_wi_verification
                                      CHECK (verification_status IN ('suggested', 'confirmed', 'rejected', 'user_created')),
          origin                      text NOT NULL CONSTRAINT ck_wi_origin CHECK (origin IN ('ai', 'user')),
          commitment_strength         text NULL CONSTRAINT ck_wi_strength
                                      CHECK (commitment_strength IN ('explicit', 'probable', 'suggestion', 'inferred')),
          statement_kind              text NULL CONSTRAINT ck_wi_statement_kind CHECK (statement_kind IN
                                      ('promise', 'request', 'acceptance', 'report_commitment', 'report_request', 'assignment')),
          direction                   text NOT NULL CONSTRAINT ck_wi_direction CHECK (direction IN
                                      ('my_task', 'my_commitment', 'delegated', 'waiting_for', 'shared', 'observed', 'unresolved')),
          notes                       text NULL,
          confidence                  real NULL,
          confidence_band             text NULL CONSTRAINT ck_wi_band CHECK (confidence_band IN ('low', 'medium', 'high')),
          reported_status             text NULL,
          reported_status_at          timestamptz NULL,
          reported_status_evidence_id uuid NULL CONSTRAINT fk_wi_reported_evidence REFERENCES evidence (id),
          extraction_id               uuid NULL CONSTRAINT fk_wi_extraction REFERENCES extractions (id),
          extraction_method           text NOT NULL CONSTRAINT ck_wi_method
                                      CHECK (extraction_method IN ('llm', 'llm_adjudicated', 'rule', 'deterministic', 'user')),
          model                       text NULL,
          derived_at                  timestamptz NOT NULL,
          user_fields                 text[] NOT NULL DEFAULT '{}',
          pending_adjudication        boolean NOT NULL DEFAULT false,
          has_conflict                boolean NOT NULL DEFAULT false,
          has_source_gap              boolean NOT NULL DEFAULT false,
          stale                       boolean NOT NULL DEFAULT false,
          archived                    boolean NOT NULL DEFAULT false,
          priority_score              real NULL,
          priority_reasons            jsonb NULL,
          priority_override           smallint NULL,
          priority_computed_at        timestamptz NULL,
          dedupe_embedding            halfvec(768) NULL,
          first_evidence_at           timestamptz NULL,
          last_activity_at            timestamptz NULL,
          state                       jsonb NOT NULL DEFAULT '{}',
          merged_into_id              uuid NULL CONSTRAINT fk_wi_merged_into REFERENCES work_items (id),
          version                     int NOT NULL DEFAULT 1,
          created_at                  timestamptz NOT NULL DEFAULT now(),
          updated_at                  timestamptz NOT NULL DEFAULT now(),
          deleted_at                  timestamptz NULL
        )
        """
    )
    for name, col in (("direction", "direction"), ("owner", "owner_person_id"), ("counterparty", "counterparty_person_id")):
        op.execute(
            f"CREATE INDEX ix_wi_{name}_open ON work_items (user_id, {col}, due_at) "
            "WHERE lifecycle_status IN ('open', 'in_progress') AND verification_status <> 'rejected' "
            "AND merged_into_id IS NULL AND NOT archived AND deleted_at IS NULL"
        )
    op.execute(
        "CREATE INDEX ix_wi_due_open ON work_items (user_id, due_at) "
        "WHERE due_at IS NOT NULL AND lifecycle_status IN ('open', 'in_progress') AND deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_wi_hint_trgm ON work_items USING gin (project_hint gin_trgm_ops) WHERE project_hint IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE decisions (
          id                  uuid PRIMARY KEY,
          user_id             uuid NOT NULL CONSTRAINT fk_decisions_user REFERENCES users (id),
          kind                text NOT NULL CONSTRAINT ck_decisions_kind CHECK (kind IN ('decision', 'open_question')),
          statement           text NOT NULL,
          rationale           text NULL,
          decided_at          timestamptz NULL,
          conversation_id     uuid NULL CONSTRAINT fk_decisions_conversation REFERENCES conversations (id),
          project_hint        text NULL,
          superseded_by_id    uuid NULL CONSTRAINT fk_decisions_superseded REFERENCES decisions (id),
          resolved_by_id      uuid NULL CONSTRAINT fk_decisions_resolved REFERENCES decisions (id),
          origin              text NOT NULL CONSTRAINT ck_decisions_origin CHECK (origin IN ('ai', 'user')),
          verification_status text NOT NULL DEFAULT 'suggested' CONSTRAINT ck_decisions_verification
                              CHECK (verification_status IN ('suggested', 'confirmed', 'rejected', 'user_created')),
          notes               text NULL,
          confidence          real NULL,
          confidence_band     text NULL,
          extraction_id       uuid NULL CONSTRAINT fk_decisions_extraction REFERENCES extractions (id),
          extraction_method   text NOT NULL,
          model               text NULL,
          derived_at          timestamptz NOT NULL,
          user_fields         text[] NOT NULL DEFAULT '{}',
          dedupe_embedding    halfvec(768) NULL,
          merged_into_id      uuid NULL CONSTRAINT fk_decisions_merged_into REFERENCES decisions (id),
          version             int NOT NULL DEFAULT 1,
          created_at          timestamptz NOT NULL DEFAULT now(),
          updated_at          timestamptz NOT NULL DEFAULT now(),
          deleted_at          timestamptz NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE item_evidence (
          item_type   text NOT NULL CONSTRAINT ck_item_evidence_type CHECK (item_type IN ('work_item', 'decision')),
          item_id     uuid NOT NULL,
          evidence_id uuid NOT NULL CONSTRAINT fk_item_evidence_evidence REFERENCES evidence (id),
          relation    text NOT NULL CONSTRAINT ck_item_evidence_relation
                      CHECK (relation IN ('supports', 'updates', 'contradicts', 'completes', 'superseded')),
          user_id     uuid NOT NULL CONSTRAINT fk_item_evidence_user REFERENCES users (id),
          created_at  timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT pk_item_evidence PRIMARY KEY (item_type, item_id, evidence_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_item_evidence_evidence ON item_evidence (evidence_id)")
    op.execute(
        """
        CREATE TABLE context_events (
          id            uuid PRIMARY KEY,
          user_id       uuid NOT NULL CONSTRAINT fk_context_events_user REFERENCES users (id),
          entity_type   text NOT NULL,
          entity_id     uuid NOT NULL,
          event_type    text NOT NULL,
          payload       jsonb NOT NULL DEFAULT '{}',
          evidence_id   uuid NULL CONSTRAINT fk_context_events_evidence REFERENCES evidence (id),
          actor         text NOT NULL CONSTRAINT ck_ce_actor CHECK (actor IN ('system', 'model', 'user', 'time')),
          authority     smallint NOT NULL CONSTRAINT ck_ce_authority CHECK (authority BETWEEN 1 AND 5),
          materiality   smallint NOT NULL CONSTRAINT ck_ce_materiality CHECK (materiality BETWEEN 0 AND 3),
          extraction_id uuid NULL CONSTRAINT fk_context_events_extraction REFERENCES extractions (id),
          dedupe_key    bytea NOT NULL,
          occurred_at   timestamptz NOT NULL,
          recorded_at   timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ux_context_events_dedupe UNIQUE (user_id, dedupe_key)
        )
        """
    )
    op.execute("CREATE INDEX ix_ce_feed ON context_events (user_id, recorded_at DESC, id DESC)")
    op.execute("CREATE INDEX ix_ce_entity ON context_events (user_id, entity_type, entity_id, occurred_at)")
    op.execute(
        "ALTER TABLE messages ADD CONSTRAINT fk_messages_triage_extraction "
        "FOREIGN KEY (triage_extraction_id) REFERENCES extractions (id)"
    )
    op.execute(
        "ALTER TABLE entity_mentions ADD CONSTRAINT fk_entity_mentions_evidence "
        "FOREIGN KEY (evidence_id) REFERENCES evidence (id)"
    )
    for table in ("extractions", "work_items", "decisions"):
        op.execute(
            f"CREATE TRIGGER tr_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION eca_set_updated_at()"
        )
    for table in TABLES:
        for stmt in user_isolation_ddl(table):
            op.execute(stmt)


def downgrade() -> None:
    op.execute("ALTER TABLE entity_mentions DROP CONSTRAINT fk_entity_mentions_evidence")
    op.execute("ALTER TABLE messages DROP CONSTRAINT fk_messages_triage_extraction")
    for table in ("context_events", "item_evidence", "decisions", "work_items", "evidence", "extractions"):
        for stmt in drop_user_isolation_ddl(table):
            op.execute(stmt)
        op.execute(f"DROP TABLE {table}")
