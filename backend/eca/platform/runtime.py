"""Process-level runtime setup."""

from __future__ import annotations

import asyncio
import sys


def ensure_selector_event_loop_policy() -> None:
    """psycopg 3 async cannot run on Windows' default ProactorEventLoop.

    Development hosts on Windows need the selector loop (BACKEND_DESIGN.md §2.2). This is a
    no-op on other platforms.
    """
    if sys.platform == "win32":
        policy = asyncio.get_event_loop_policy()
        if not isinstance(policy, asyncio.WindowsSelectorEventLoopPolicy):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Event loop factory for uvicorn: ``--loop eca.platform.runtime:selector_loop_factory``.

    uvicorn's default asyncio setup creates a ProactorEventLoop on Windows regardless of the loop
    policy, which psycopg 3 async cannot use. On Linux this is the standard selector loop.
    """
    return asyncio.SelectorEventLoop()
