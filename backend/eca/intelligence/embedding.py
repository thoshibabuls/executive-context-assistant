"""AI-04 ``embed``: document and query embeddings (AI_PIPELINE.md §3; CONTEXT_ARCHITECTURE.md §9.10).

The input format is versioned as ``embed/v1`` (it is part of every cassette key) and the vectors
are L2-normalized here, so cosine distance in pgvector equals 1 - dot product. Callers store
``model`` with every vector and filter on it, so a registry model change never mixes vectors.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from eca.intelligence.provider.client import AIClient
from eca.intelligence.provider.types import AIError, ProviderError, SchemaInvalid
from eca.platform.errors import BudgetExceeded

EMBED_ROLE = "embed"
EMBED_INPUT_VERSION = "embed/v1"
MAX_EMBED_BATCH = 100


class EmbeddingUnavailable(AIError):
    """The embedding call failed; callers fall back to full-text search (AI_PIPELINE.md §14)."""

    def __init__(self, error_type: str) -> None:
        super().__init__(f"embedding unavailable after {error_type}")
        self.error_type = error_type


@dataclass(frozen=True)
class Embeddings:
    vectors: tuple[tuple[float, ...], ...]
    model: str
    call_ids: tuple[UUID, ...] = field(default_factory=tuple)


def document_input(title: str | None, text: str) -> str:
    return f"title: {title or 'none'} | text: {text}"


def query_input(question: str) -> str:
    return f"task: search result | query: {question}"


def input_digest(embedding_input: str) -> bytes:
    """``chunks.content_hash``: unchanged input → no new embedding call."""
    return hashlib.sha256(f"{EMBED_INPUT_VERSION}\n{embedding_input}".encode()).digest()


def normalize(vector: Sequence[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return tuple(float(x) for x in vector)
    return tuple(float(x) / norm for x in vector)


def embedding_model(client: AIClient) -> str:
    """The model of the enabled ``embed`` role (raises ``RoleDisabled``/``UnknownRole``)."""
    return client.registry.role(EMBED_ROLE).model


async def embed_documents(
    client: AIClient, inputs: Sequence[str], *, user_id: UUID | None, attempt: int = 1
) -> Embeddings:
    """Embed document inputs in batches of at most ``MAX_EMBED_BATCH``. One call per batch."""
    vectors: list[tuple[float, ...]] = []
    calls: list[UUID] = []
    model = embedding_model(client)
    for start in range(0, len(inputs), MAX_EMBED_BATCH):
        batch = list(inputs[start : start + MAX_EMBED_BATCH])
        try:
            result = await client.embed(
                EMBED_ROLE, batch, prompt_version=EMBED_INPUT_VERSION, user_id=user_id, attempt=attempt
            )
        except (ProviderError, SchemaInvalid) as exc:
            raise EmbeddingUnavailable(type(exc).__name__) from exc
        except BudgetExceeded as exc:  # hard cap or global budget: FTS-only (AI_COST_MODEL.md §7.2)
            raise EmbeddingUnavailable("budget_exceeded") from exc
        if len(result.vectors) != len(batch):
            raise EmbeddingUnavailable("vector_count_mismatch")
        vectors.extend(normalize(v) for v in result.vectors)
        model = result.model
        if result.call_id is not None:
            calls.append(result.call_id)
    return Embeddings(tuple(vectors), model, tuple(calls))


async def embed_query(client: AIClient, question: str, *, user_id: UUID | None) -> Embeddings:
    """Interactive query embedding: one retry (the role has no fallback model), then unavailable."""
    last: EmbeddingUnavailable | None = None
    for attempt in (1, 2):
        try:
            return await embed_documents(client, [query_input(question)], user_id=user_id, attempt=attempt)
        except EmbeddingUnavailable as exc:
            last = exc
            if exc.error_type == "budget_exceeded":
                break
    assert last is not None
    raise last


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector text form, cast in SQL with ``CAST(:v AS halfvec)``."""
    return "[" + ",".join(f"{x:.6g}" for x in vector) + "]"
