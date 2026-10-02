"""Deterministic crash points for the reliability tests (BACKEND_DESIGN.md §21, crash-test harness).

The hooks are no-ops unless ``ECA_TEST_CRASH_POINT`` names one of :data:`CRASH_POINTS`. The
variable is read once by :func:`configure` at process start. An unknown name, or any value while
``API_ENV`` is production, is a startup error. An armed point ends the process with
``os._exit(97)`` (no cleanup, no flushing) the first time it is reached.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal, get_args

CrashPoint = Literal[
    "dispatch.after_claim",
    "dispatch.after_defer",
    "dispatch.before_commit",
    "handler.after_consumption",
    "handler.before_commit",
    "handler.after_commit",
]

CRASH_POINTS: frozenset[str] = frozenset(get_args(CrashPoint))
ENV_VAR = "ECA_TEST_CRASH_POINT"
EXIT_CODE = 97

_armed: str | None = None


class CrashPointConfigError(RuntimeError):
    """The crash-point variable is invalid or set where it is forbidden; the process must not start."""


def configure(*, is_production: bool, environ: Mapping[str, str] | None = None) -> str | None:
    """Arm the crash point named in the environment, or disarm all. Returns the armed name."""
    global _armed
    value = (os.environ if environ is None else environ).get(ENV_VAR, "")
    _armed = None
    if not value:
        return None
    if is_production:
        raise CrashPointConfigError(f"{ENV_VAR} must not be set in production")
    if value not in CRASH_POINTS:
        raise CrashPointConfigError(f"{ENV_VAR}={value!r} is not a known crash point")
    _armed = value
    return value


def hit(point: CrashPoint) -> None:
    """Crash here if this point is armed."""
    if _armed == point:
        os._exit(EXIT_CODE)
