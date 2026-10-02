"""Simulated clock for replay (CONTEXT_EVALUATION.md §4.1, AI_EVALUATION.md §11)."""

from __future__ import annotations

import datetime
from collections.abc import Iterator


class SimulatedClock:
    """A clock that only moves when told to. Never goes backwards."""

    def __init__(self, start: datetime.datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._now = start

    def now(self) -> datetime.datetime:
        return self._now

    def advance_to(self, moment: datetime.datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone-aware")
        if moment < self._now:
            raise ValueError("the simulated clock never goes backwards")
        self._now = moment

    def advance(self, delta: datetime.timedelta) -> None:
        if delta < datetime.timedelta(0):
            raise ValueError("delta must not be negative")
        self._now += delta

    def ticks_until(
        self, moment: datetime.datetime, step: datetime.timedelta = datetime.timedelta(hours=1)
    ) -> Iterator[datetime.datetime]:
        """Advance in ``step`` increments (hourly sweeps) up to ``moment``, yielding each tick."""
        if step <= datetime.timedelta(0):
            raise ValueError("step must be positive")
        while self._now + step <= moment:
            self._now += step
            yield self._now
        self.advance_to(moment)
