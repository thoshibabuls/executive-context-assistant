"""Job queues and their per-worker concurrency (BACKEND_DESIGN.md §15)."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

QUEUE_CONCURRENCY: Mapping[str, int] = MappingProxyType(
    {
        "events": 8,
        "sync": 4,
        "ingest": 8,
        "extract": 8,
        "apply": 8,
        "embed": 2,
        "ai_standard": 4,
        "media": 1,
        "schedule": 1,
    }
)
