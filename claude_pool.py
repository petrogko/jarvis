"""
Global concurrency cap for ``claude -p`` subprocess spawns.

Threat:
    Every call into Claude Code is a fork + Anthropic API round-trip
    + filesystem writes. With no global cap, a buggy frontend that
    reconnects in a loop, an abusive LAN client (when ``--host`` is
    opened up), or even a self-referential prompt-injection chain
    could fork-bomb the host. The original ``ClaudeTaskManager``
    capped its own queue (default 3) but four other spawn sites in
    the code (``_execute_research``, ``WorkSession.send``, two
    paths in ``qa.py``) bypassed it.

Design:
    One module-level counter, bounded by
    ``JARVIS_MAX_CONCURRENT_CLAUDE`` (default 5). Two acquisition
    shapes:

    * ``acquire_immediate()`` — for HTTP-triggered spawns
      (``POST /api/tasks``). Returns False right away if the pool
      is full so the endpoint can answer 429.
    * ``acquire()`` — async context manager for background spawns
      (research, QA, work-mode). Awaits a slot.

    Both account against the same budget. ``active()`` and
    ``capacity()`` expose the state for status endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

log = logging.getLogger("jarvis.claude_pool")


def _read_max_concurrent() -> int:
    raw = os.getenv("JARVIS_MAX_CONCURRENT_CLAUDE", "5")
    try:
        n = int(raw)
    except ValueError:
        log.warning("JARVIS_MAX_CONCURRENT_CLAUDE=%r is not an int; defaulting to 5", raw)
        return 5
    return max(1, min(n, 32))


MAX_CONCURRENT = _read_max_concurrent()

# Single counter + condition variable. asyncio.Condition gives us both
# the "try without waiting" shape and the "await a slot" shape against
# the same budget without poking at semaphore internals.
_in_flight = 0
_cond: asyncio.Condition | None = None


def _get_cond() -> asyncio.Condition:
    global _cond
    if _cond is None:
        _cond = asyncio.Condition()
    return _cond


def active() -> int:
    """Current number of in-flight ``claude -p`` invocations."""
    return _in_flight


def capacity() -> int:
    return MAX_CONCURRENT


async def acquire_immediate() -> bool:
    """Non-blocking acquire. Returns True if a slot was taken, False if full.

    The caller MUST call ``release()`` exactly once on success.
    """
    global _in_flight
    cond = _get_cond()
    async with cond:
        if _in_flight >= MAX_CONCURRENT:
            return False
        _in_flight += 1
        return True


async def release() -> None:
    """Release a slot previously acquired by ``acquire_immediate()``."""
    global _in_flight
    cond = _get_cond()
    async with cond:
        if _in_flight > 0:
            _in_flight -= 1
        cond.notify(1)


@asynccontextmanager
async def acquire():
    """Async context manager — awaits a slot, then releases on exit.

    Use this from background tasks that can politely queue.
    """
    global _in_flight
    cond = _get_cond()
    async with cond:
        while _in_flight >= MAX_CONCURRENT:
            await cond.wait()
        _in_flight += 1
    try:
        yield
    finally:
        async with cond:
            if _in_flight > 0:
                _in_flight -= 1
            cond.notify(1)
