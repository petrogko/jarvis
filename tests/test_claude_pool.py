"""
Tests for claude_pool — the global cap on ``claude -p`` spawns.

Verifies:
- acquire_immediate succeeds when under cap, fails when at cap.
- release() returns the slot.
- acquire() (async ctx mgr) waits for a slot.
- in_flight count is accurate across mixed acquire types.
- cap is sourced from JARVIS_MAX_CONCURRENT_CLAUDE env var.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reload_with_cap(cap: int):
    os.environ["JARVIS_MAX_CONCURRENT_CLAUDE"] = str(cap)
    if "claude_pool" in sys.modules:
        del sys.modules["claude_pool"]
    import claude_pool as cp  # noqa: WPS433
    return cp


def test_env_var_sets_cap():
    cp = _reload_with_cap(7)
    assert cp.capacity() == 7


def test_env_var_clamps_to_minimum():
    cp = _reload_with_cap(0)
    assert cp.capacity() == 1


def test_env_var_clamps_to_maximum():
    cp = _reload_with_cap(999)
    assert cp.capacity() == 32


def test_acquire_immediate_under_cap_succeeds():
    cp = _reload_with_cap(2)

    async def run():
        assert await cp.acquire_immediate() is True
        assert cp.active() == 1
        assert await cp.acquire_immediate() is True
        assert cp.active() == 2

    asyncio.run(run())


def test_acquire_immediate_at_cap_fails():
    cp = _reload_with_cap(1)

    async def run():
        assert await cp.acquire_immediate() is True
        assert await cp.acquire_immediate() is False
        await cp.release()
        # Slot freed — next acquire should succeed
        assert await cp.acquire_immediate() is True

    asyncio.run(run())


def test_acquire_context_manager_waits():
    cp = _reload_with_cap(1)
    timeline: list[str] = []

    async def first():
        async with cp.acquire():
            timeline.append("first-in")
            await asyncio.sleep(0.05)
            timeline.append("first-out")

    async def second():
        # Give first a head start so it acquires first
        await asyncio.sleep(0.01)
        async with cp.acquire():
            timeline.append("second-in")

    async def run():
        await asyncio.gather(first(), second())

    asyncio.run(run())
    # second must enter only after first exits
    assert timeline == ["first-in", "first-out", "second-in"], timeline


def test_release_decrements_in_flight():
    cp = _reload_with_cap(3)

    async def run():
        await cp.acquire_immediate()
        await cp.acquire_immediate()
        assert cp.active() == 2
        await cp.release()
        assert cp.active() == 1
        await cp.release()
        assert cp.active() == 0

    asyncio.run(run())
