"""Tables owned by ``retrieval`` (BACKEND_DESIGN.md §5.1, §17.5). Mirrors migration 0011.

``embedding`` (``halfvec(768)``) and ``tsv`` (generated) are not declared: they are written and
read only through the explicit SQL in ``indexing`` and ``search``, which casts the vector text.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()
_U = PG_UUID(as_uuid=True)
_TS = DateTime(timezone=True)

chunks_table = Table(
    "chunks",
    metadata,
    Column("id", _U, primary_key=True),
    Column("user_id", _U, nullable=False),
    Column("source_item_id", _U, nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("title", Text),
    Column("text", Text, nullable=False),
    Column("token_count", Integer, nullable=False),
    Column("content_hash", BYTEA, nullable=False),
    Column("occurred_at", _TS, nullable=False),
    Column("conversation_id", _U),
    Column("meeting_id", _U),
    Column("person_ids", ARRAY(_U), nullable=False),
    Column("embedding_model", Text),
    Column("embedded_at", _TS),
    Column("created_at", _TS),
    Column("updated_at", _TS),
)
