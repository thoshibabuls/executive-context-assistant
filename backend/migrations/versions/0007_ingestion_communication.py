"""Slice 1.3: source items with the stage machine, conversations, messages, participants and
entity mentions.

BACKEND_DESIGN.md §7.5 (stage machine), §10.1 (idempotency keys), §17.2 (DDL) and the Batch A
schema decisions in §17 (``source_items.content``); TECHNICAL_DESIGN.md §9.1 (columns). Every
table is a user-owned business table under ``<table>_user_isolation``.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("source_items", "conversations", "messages", "message_participants", "entity_mentions")


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
        CREATE TABLE source_items (
          id                 uuid PRIMARY KEY,
          user_id            uuid NOT NULL CONSTRAINT fk_source_items_user REFERENCES users (id),
          connection_id      uuid NULL CONSTRAINT fk_source_items_connection REFERENCES connections (id),
          kind               text NOT NULL CONSTRAINT ck_source_items_kind
                             CHECK (kind IN ('message', 'calendar_event', 'recording', 'transcript_file')),
          provider           text NOT NULL,
          external_id        text NOT NULL,
          external_thread_id text NULL,
          content_hash       bytea NOT NULL,
          provider_version   text NULL,
          categories         text[] NOT NULL DEFAULT '{}',
          occurred_at        timestamptz NOT NULL,
          trashed            boolean NOT NULL DEFAULT false,
          content            jsonb NULL,
          stage              text NOT NULL DEFAULT 'fetched' CONSTRAINT ck_source_items_stage
                             CHECK (stage IN ('fetched', 'normalized', 'skipped', 'extract_pending',
                                              'extracted', 'applied', 'needs_attention')),
          stage_attempts     smallint NOT NULL DEFAULT 0,
          stage_updated_at   timestamptz NOT NULL DEFAULT now(),
          next_attempt_at    timestamptz NULL,
          last_error_code    text NULL,
          raw_metadata       jsonb NOT NULL DEFAULT '{}',
          created_at         timestamptz NOT NULL DEFAULT now(),
          updated_at         timestamptz NOT NULL DEFAULT now(),
          deleted_at         timestamptz NULL
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_source_items_ext ON source_items (connection_id, kind, external_id)")
    op.execute(
        "CREATE INDEX ix_source_items_stage ON source_items (stage, next_attempt_at) "
        "WHERE stage IN ('fetched', 'extract_pending', 'extracted') AND deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_source_items_user_time ON source_items (user_id, occurred_at DESC) WHERE deleted_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE conversations (
          id                         uuid PRIMARY KEY,
          user_id                    uuid NOT NULL CONSTRAINT fk_conversations_user REFERENCES users (id),
          connection_id              uuid NULL CONSTRAINT fk_conversations_connection REFERENCES connections (id),
          kind                       text NOT NULL DEFAULT 'email_thread' CONSTRAINT ck_conversations_kind
                                     CHECK (kind IN ('email_thread', 'chat_thread', 'channel')),
          external_thread_id         text NOT NULL,
          subject                    text NULL,
          first_message_at           timestamptz NULL,
          last_message_at            timestamptz NULL,
          last_inbound_at            timestamptz NULL,
          last_outbound_at           timestamptz NULL,
          awaiting                   text NOT NULL DEFAULT 'none' CONSTRAINT ck_conversations_awaiting
                                     CHECK (awaiting IN ('user', 'other', 'none')),
          needs_reply                boolean NOT NULL DEFAULT false,
          needs_reply_source         text NULL CONSTRAINT ck_conversations_needs_reply_source
                                     CHECK (needs_reply_source IN ('heuristic', 'triage', 'user')),
          handled_by_user_at         timestamptz NULL,
          summary                    text NULL,
          summary_through_message_id uuid NULL,
          priority_score             real NULL,
          priority_reasons           jsonb NULL,
          priority_override          smallint NULL,
          version                    int NOT NULL DEFAULT 1,
          created_at                 timestamptz NOT NULL DEFAULT now(),
          updated_at                 timestamptz NOT NULL DEFAULT now(),
          deleted_at                 timestamptz NULL,
          CONSTRAINT ux_conversations_thread UNIQUE (user_id, connection_id, kind, external_thread_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_conv_needs_reply ON conversations (user_id, priority_score DESC, last_inbound_at DESC, id) "
        "WHERE awaiting = 'user' AND needs_reply AND handled_by_user_at IS NULL AND deleted_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE messages (
          id                    uuid PRIMARY KEY,
          user_id               uuid NOT NULL CONSTRAINT fk_messages_user REFERENCES users (id),
          source_item_id        uuid NOT NULL CONSTRAINT ux_messages_source_item UNIQUE
                                CONSTRAINT fk_messages_source_item REFERENCES source_items (id),
          conversation_id       uuid NOT NULL CONSTRAINT fk_messages_conversation REFERENCES conversations (id),
          rfc822_message_id     text NULL,
          in_reply_to           text NULL,
          sender_person_id      uuid NULL CONSTRAINT fk_messages_sender REFERENCES persons (id),
          direction             text NOT NULL CONSTRAINT ck_messages_direction CHECK (direction IN ('inbound', 'outbound')),
          sent_at               timestamptz NOT NULL,
          subject               text NULL,
          body_text             text NULL,
          body_clean            text NULL,
          body_purged_at        timestamptz NULL,
          snippet               text NULL,
          is_bulk               boolean NOT NULL DEFAULT false,
          prefilter_reason      text NULL,
          triage                jsonb NULL,
          triage_extraction_id  uuid NULL,
          alias_source_item_ids uuid[] NOT NULL DEFAULT '{}',
          created_at            timestamptz NOT NULL DEFAULT now(),
          updated_at            timestamptz NOT NULL DEFAULT now(),
          deleted_at            timestamptz NULL
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_messages_rfc822 ON messages (user_id, rfc822_message_id) "
        "WHERE rfc822_message_id IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_messages_conv_time ON messages (conversation_id, sent_at)")
    op.execute(
        "CREATE INDEX ix_messages_sender_time ON messages (user_id, sender_person_id, sent_at DESC) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE message_participants (
          message_id uuid NOT NULL CONSTRAINT fk_message_participants_message REFERENCES messages (id),
          person_id  uuid NOT NULL CONSTRAINT fk_message_participants_person REFERENCES persons (id),
          role       text NOT NULL CONSTRAINT ck_message_participants_role CHECK (role IN ('from', 'to', 'cc', 'bcc')),
          user_id    uuid NOT NULL CONSTRAINT fk_message_participants_user REFERENCES users (id),
          CONSTRAINT pk_message_participants PRIMARY KEY (message_id, person_id, role)
        )
        """
    )
    op.execute("CREATE INDEX ix_message_participants_person ON message_participants (user_id, person_id)")
    op.execute(
        """
        CREATE TABLE entity_mentions (
          id             uuid PRIMARY KEY,
          user_id        uuid NOT NULL CONSTRAINT fk_entity_mentions_user REFERENCES users (id),
          source_item_id uuid NOT NULL CONSTRAINT fk_entity_mentions_source_item REFERENCES source_items (id),
          chunk_id       uuid NULL,
          evidence_id    uuid NULL,
          entity_type    text NOT NULL CONSTRAINT ck_entity_mentions_type CHECK (entity_type IN ('person', 'organization', 'project')),
          entity_id      uuid NOT NULL,
          surface_text   text NOT NULL,
          confidence     real NOT NULL,
          method         text NOT NULL CONSTRAINT ck_entity_mentions_method
                         CHECK (method IN ('header', 'alias_match', 'extraction', 'user')),
          occurred_at    timestamptz NOT NULL,
          created_at     timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ux_entity_mentions UNIQUE (user_id, source_item_id, entity_type, entity_id, method, surface_text)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_entity_mentions ON entity_mentions (user_id, entity_type, entity_id, occurred_at DESC)"
    )

    for table in ("source_items", "conversations", "messages"):
        op.execute(
            f"CREATE TRIGGER tr_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION eca_set_updated_at()"
        )
    for table in TABLES:
        for stmt in user_isolation_ddl(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in reversed(TABLES):
        for stmt in drop_user_isolation_ddl(table):
            op.execute(stmt)
        op.execute(f"DROP TABLE {table}")
