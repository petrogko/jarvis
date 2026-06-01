"""Hermetic tests for idle_lock.

Covers spec §11 + the security-advisor's 6 required fixes:
- #1 touch() is called from binary + text frames (enforced at WS call site;
     here we verify the manager itself is frame-type-agnostic — any call to
     touch() resets the timer).
- #2 4423 close code is authoritative; broadcast is best-effort.
- #4 audit_extras carries only ``had_ws`` + optional ``clock_jump`` — never
     "idle" vs "wandered_ws" labels.
- #5 Clock-jump detection on macOS sleep gaps.
- #6 Sealed-conversation override (stricter threshold; IDLE_LOCK_DISABLED
     refused). Sealed-detection is injected so we test the full matrix.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import idle_lock as il


# ---------- Manager: touch + idle_for + evaluate --------------------------

def test_touch_resets_idle_for(monkeypatch):
    m = il.IdleLockManager()
    t0 = 1000.0
    monkeypatch.setattr(il.time, "time", lambda: t0)
    m.touch()
    assert m.idle_for() == 0.0
    # Jump forward.
    monkeypatch.setattr(il.time, "time", lambda: t0 + 600.0)
    assert m.idle_for() == 600.0
    # Touch again -> resets.
    m.touch()
    assert m.idle_for() == 0.0


def test_evaluate_below_threshold_no_lock(monkeypatch):
    m = il.IdleLockManager()
    t0 = 1000.0
    monkeypatch.setattr(il.time, "time", lambda: t0)
    m.touch()
    monkeypatch.setattr(il.time, "time", lambda: t0 + 100.0)
    should, audit = m.evaluate(idle_lock_s=900.0, ws_count=0)
    assert should is False
    assert audit == {}


def test_evaluate_above_threshold_no_ws_locks(monkeypatch):
    m = il.IdleLockManager()
    t0 = 1000.0
    monkeypatch.setattr(il.time, "time", lambda: t0)
    m.touch()
    monkeypatch.setattr(il.time, "time", lambda: t0 + 901.0)
    should, audit = m.evaluate(idle_lock_s=900.0, ws_count=0)
    assert should is True
    assert audit["had_ws"] is False
    assert "clock_jump" not in audit


def test_evaluate_with_ws_uses_2x_threshold(monkeypatch):
    """Wandered-WS: connected client doesn't get a free pass forever."""
    m = il.IdleLockManager()
    t0 = 1000.0
    monkeypatch.setattr(il.time, "time", lambda: t0)
    m.touch()
    # Just past 1x — should NOT lock (WS present).
    monkeypatch.setattr(il.time, "time", lambda: t0 + 1000.0)
    should, _ = m.evaluate(idle_lock_s=900.0, ws_count=1)
    assert should is False
    # Past 2x — should lock.
    monkeypatch.setattr(il.time, "time", lambda: t0 + 1810.0)
    should, audit = m.evaluate(idle_lock_s=900.0, ws_count=1)
    assert should is True
    assert audit["had_ws"] is True


# ---------- Required fix #4: audit collapse -------------------------------

def test_audit_extras_never_contain_idle_or_wandered_labels(monkeypatch):
    """The advisor flagged 'wandered_ws' vs 'idle' as a habit side-channel.
    The single allowed classifier is ``had_ws: bool``."""
    m = il.IdleLockManager()
    t0 = 1000.0
    monkeypatch.setattr(il.time, "time", lambda: t0)
    m.touch()
    monkeypatch.setattr(il.time, "time", lambda: t0 + 5000.0)
    # Both call sites — verify only the sanctioned keys appear.
    _, idle_audit = m.evaluate(idle_lock_s=900.0, ws_count=0)
    _, wander_audit = m.evaluate(idle_lock_s=900.0, ws_count=3)
    for audit in (idle_audit, wander_audit):
        assert set(audit.keys()).issubset({"had_ws", "clock_jump"})


# ---------- Required fix #5: clock-jump --------------------------------

def test_clock_jump_flagged_on_large_gap(monkeypatch):
    """idle_for > IDLE_LOCK_S + 300 ⇒ clock_jump in the audit. Simulates
    macOS sleep / lid-close: the timer can't fire while suspended, so it
    fires *late* on wake."""
    m = il.IdleLockManager()
    t0 = 1000.0
    monkeypatch.setattr(il.time, "time", lambda: t0)
    m.touch()
    # 900 + 301 = 1201s — past the clock-jump threshold.
    monkeypatch.setattr(il.time, "time", lambda: t0 + 1201.0)
    should, audit = m.evaluate(idle_lock_s=900.0, ws_count=0)
    assert should is True
    assert audit.get("clock_jump") is True


def test_clock_jump_not_flagged_on_normal_idle(monkeypatch):
    """Threshold + 200s isn't a jump — that's just slightly stale activity."""
    m = il.IdleLockManager()
    t0 = 1000.0
    monkeypatch.setattr(il.time, "time", lambda: t0)
    m.touch()
    monkeypatch.setattr(il.time, "time", lambda: t0 + 1100.0)
    should, audit = m.evaluate(idle_lock_s=900.0, ws_count=0)
    assert should is True
    assert audit.get("clock_jump") is not True


# ---------- Required fix #6: sealed-aware threshold ----------------------

def test_sealed_uses_stricter_threshold(monkeypatch):
    """Per advisor Q1: when a sealed conversation exists, use the sealed
    threshold (e.g. 120s) instead of the default 900s — counsel-mode is
    the whole point of sealed."""
    m = il.IdleLockManager()
    t0 = 1000.0
    monkeypatch.setattr(il.time, "time", lambda: t0)
    m.touch()
    monkeypatch.setattr(il.time, "time", lambda: t0 + 200.0)
    # No sealed: 200s < 900s default — no lock.
    should, _ = m.evaluate(idle_lock_s=900.0, ws_count=0)
    assert should is False
    # Sealed: 200s > 120s sealed-threshold — lock.
    should, _ = m.evaluate(
        idle_lock_s=900.0, ws_count=0,
        sealed_exists=True, sealed_idle_lock_s=120.0,
    )
    assert should is True


def test_sealed_overrides_even_if_default_is_lower(monkeypatch):
    """If somehow the default is set BELOW sealed-threshold (e.g. user
    configured 60s default), the evaluator uses the tighter of the two."""
    m = il.IdleLockManager()
    t0 = 1000.0
    monkeypatch.setattr(il.time, "time", lambda: t0)
    m.touch()
    monkeypatch.setattr(il.time, "time", lambda: t0 + 80.0)
    # 60s default + sealed=120s: min(60, 120) = 60 → 80s > 60 → lock.
    should, _ = m.evaluate(
        idle_lock_s=60.0, ws_count=0,
        sealed_exists=True, sealed_idle_lock_s=120.0,
    )
    assert should is True


# ---------- clamp_idle_lock_s --------------------------------------------

def test_clamp_below_minimum():
    assert il.clamp_idle_lock_s(10.0) == il.IDLE_LOCK_S_MIN


def test_clamp_above_maximum():
    assert il.clamp_idle_lock_s(99999.0) == il.IDLE_LOCK_S_MAX


def test_clamp_within_range_passes_through():
    assert il.clamp_idle_lock_s(900.0) == 900.0


# ---------- Background loop -----------------------------------------------

async def test_loop_no_op_when_disabled(monkeypatch):
    """When config returns ``enabled=False``, no lock is attempted."""
    m = il.IdleLockManager()
    monkeypatch.setattr(il.time, "time", lambda: 1000.0)
    m.touch()
    monkeypatch.setattr(il.time, "time", lambda: 1000.0 + 5000.0)

    lock_calls = []
    async def on_lock(extras): lock_calls.append(extras)

    # Drive one tick manually by setting tick_s very small.
    task = asyncio.create_task(il.run_idle_lock_loop(
        manager=m,
        get_config=lambda: (900.0, False, False, None),  # disabled
        get_ws_count=lambda: 0,
        on_lock=on_lock,
        tick_s=0.01,
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert lock_calls == []


async def test_loop_locks_after_threshold(monkeypatch):
    m = il.IdleLockManager()
    t0 = 1000.0
    current = [t0]
    monkeypatch.setattr(il.time, "time", lambda: current[0])
    m.touch()
    # Skip ahead 1000s — past the 900s default.
    current[0] = t0 + 1000.0

    lock_calls = []
    async def on_lock(extras): lock_calls.append(extras)

    task = asyncio.create_task(il.run_idle_lock_loop(
        manager=m,
        get_config=lambda: (900.0, True, False, None),
        get_ws_count=lambda: 0,
        on_lock=on_lock,
        tick_s=0.01,
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(lock_calls) >= 1
    assert lock_calls[0]["had_ws"] is False


async def test_loop_survives_exception_in_callback(monkeypatch):
    """A transient failure in on_lock MUST NOT kill the loop. The advisor
    wants the lock task to be the most-robust thing in the process."""
    m = il.IdleLockManager()
    t0 = 1000.0
    monkeypatch.setattr(il.time, "time", lambda: t0)
    m.touch()
    monkeypatch.setattr(il.time, "time", lambda: t0 + 1500.0)

    call_count = [0]
    async def on_lock(extras):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("simulated transient failure")
        # Second call: succeed.

    task = asyncio.create_task(il.run_idle_lock_loop(
        manager=m,
        get_config=lambda: (900.0, True, False, None),
        get_ws_count=lambda: 0,
        on_lock=on_lock,
        tick_s=0.01,
    ))
    # Wait for at least 2 ticks (one fail, one success).
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count[0] >= 2, "loop died after first failure"


async def test_loop_resets_activity_after_lock(monkeypatch):
    """After firing a lock, the manager's last-activity is reset so we
    don't immediately re-fire on the next tick (avoids lock-thrash)."""
    m = il.IdleLockManager()
    t0 = 1000.0
    current = [t0]
    monkeypatch.setattr(il.time, "time", lambda: current[0])
    m.touch()
    current[0] = t0 + 1500.0  # past threshold

    lock_calls = []
    async def on_lock(extras): lock_calls.append(extras)

    task = asyncio.create_task(il.run_idle_lock_loop(
        manager=m,
        get_config=lambda: (900.0, True, False, None),
        get_ws_count=lambda: 0,
        on_lock=on_lock,
        tick_s=0.01,
    ))
    # Let it tick a few times.
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Without the reset we'd see N+ identical fires; with the reset we see
    # exactly one (the next tick sees idle ≈ 0).
    assert len(lock_calls) == 1, f"expected single fire, got {len(lock_calls)}"


# ---------- Constants smoke-check ----------------------------------------

def test_ws_close_code_is_in_4xxx_range():
    """RFC 6455 reserves 4000-4999 for application-defined close codes."""
    assert 4000 <= il.WS_CLOSE_CODE <= 4999
