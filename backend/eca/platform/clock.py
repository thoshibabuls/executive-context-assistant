"""Clock resource: the only source of "now" for pipeline code that must be replayable.

Production uses :class:`SystemClock`. Evaluation replays and tests pass a :class:`ManualClock`
through ``Resources`` so the same code runs under simulated time (CONTEXT_EVALUATION.md §4.1).
"""

from __future__ import annotations

import datetime


class Clock:
    def now(self) -> datetime.datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)


class ManualClock(Clock):
    """A clock that moves only when told to; never backwards."""

    def __init__(self, start: datetime.datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._now = start

    def now(self) -> datetime.datetime:
        return self._now

    def set(self, moment: datetime.datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone-aware")
        if moment < self._now:
            raise ValueError("the clock never goes backwards")
        self._now = moment
