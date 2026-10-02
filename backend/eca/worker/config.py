"""Worker configuration (BACKEND_DESIGN.md §14.3, §15, §21)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from eca.platform.dispatch import DispatchPolicy
from eca.platform.jobs import HandlerRetryStrategy
from eca.platform.queues import QUEUE_CONCURRENCY


@dataclass(frozen=True)
class WorkerConfig:
    """Everything the worker needs besides settings and the registry.

    The production defaults follow the design documents. Only test code builds other values
    (shorter timings); the production entry point always uses ``WorkerConfig()``.
    """

    queues: Mapping[str, int] = field(default_factory=lambda: dict(QUEUE_CONCURRENCY))
    dispatch: DispatchPolicy = field(default_factory=DispatchPolicy)
    handler_retry: HandlerRetryStrategy = field(default_factory=HandlerRetryStrategy)
    heartbeat_interval_s: float = 10.0
    stalled_timeout_s: float = 30.0
    fetch_job_polling_interval_s: float = 5.0
    shutdown_graceful_timeout_s: float = 30.0
    reconcile_min_age_s: float = 60.0
    db_pool_size: int = 10
    db_max_overflow: int = 20

    def __post_init__(self) -> None:
        unknown = set(self.queues) - set(QUEUE_CONCURRENCY)
        if unknown:
            raise ValueError(f"Unknown queues: {sorted(unknown)}")
        if any(c < 1 for c in self.queues.values()):
            raise ValueError("Queue concurrency must be >= 1")
        if self.stalled_timeout_s <= self.heartbeat_interval_s:
            raise ValueError("stalled_timeout_s must exceed heartbeat_interval_s")

    @property
    def job_pool_max_size(self) -> int:
        # One listening connection per queue worker, plus headroom for fetches, finishes and defers.
        return len(self.queues) + 10
