"""Per-user inbound rate limits in the API process (BACKEND_DESIGN.md §16.6).

In-memory token buckets: with at most 2 API instances the effective limit is at most twice the
configured value, which is acceptable because AI spend is bounded separately by budget caps.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from eca.platform.errors import RateLimited

LIMITS_PER_MINUTE = {"chat": 20, "reply_guidance": 10, "summary": 10}


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(
        self, limits: dict[str, int] | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limits = dict(limits or LIMITS_PER_MINUTE)
        self._clock = clock
        self._buckets: dict[tuple[UUID, str], _Bucket] = {}

    def check(self, user_id: UUID, name: str) -> None:
        """Take one token or raise ``RateLimited`` with the seconds until the next token."""
        per_minute = self._limits[name]
        rate = per_minute / 60.0
        now = self._clock()
        bucket = self._buckets.get((user_id, name))
        if bucket is None:
            bucket = _Bucket(float(per_minute), now)
            self._buckets[(user_id, name)] = bucket
        bucket.tokens = min(float(per_minute), bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now
        if bucket.tokens < 1.0:
            raise RateLimited(
                "too many requests", retry_after_s=max(1, math.ceil((1.0 - bucket.tokens) / rate))
            )
        bucket.tokens -= 1.0
