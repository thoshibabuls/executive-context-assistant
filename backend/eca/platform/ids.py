"""Application-generated identifiers (BACKEND_DESIGN.md §2.2: UUIDv7, time-ordered)."""

from __future__ import annotations

import os
import time
from uuid import UUID


def uuid7(*, unix_ms: int | None = None) -> UUID:
    """A version 7 UUID (RFC 9562): 48-bit Unix milliseconds, then 74 random bits."""
    ms = time.time_ns() // 1_000_000 if unix_ms is None else unix_ms
    if not 0 <= ms < 1 << 48:
        raise ValueError("timestamp out of range for UUIDv7")
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand >> 68  # 12 bits
    rand_b = rand & ((1 << 62) - 1)  # 62 bits
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return UUID(int=value)
