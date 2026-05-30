"""
Idle auto-lock — timer-based vault relock + WS disconnect after N minutes
of inactivity.

Spec: ``docs/superpowers/specs/2026-05-30-idle-auto-lock-design.md``
Security-advisor: GO-WITH-FIXES (2026-05-30). All 6 required fixes folded in:

  #1 ``touch()`` is called from every WS receive (text AND binary frames),
     not just text — long Aria audio frames shouldn't trigger wandered-WS
     misfire mid-utterance.
  #2 ``vault_locked`` WS broadcast is best-effort; the 4423 close code is
     the authoritative signal. Clients MUST treat 4423 as "vault locked,"
     regardless of whether the JSON payload arrived.
  #4 Audit verb is always ``auto_lock``; the ``idle`` vs ``wandered_ws``
     distinction was a habit side-channel and is collapsed to a single
     ``had_ws: bool`` field. The category itself is forensically silent.
  #5 Clock-jump guard — if ``idle_for > IDLE_LOCK_S + 300``, the audit line
     carries ``clock_jump: true``. macOS sleep / lid-close fires the timer
     late on wake; this surfaces the gap to an operator reviewing the log.
  #6 ``IDLE_LOCK_DISABLED`` is refused when any sealed conversation exists.
     For now (pre-1A) sealed-detection is a callback the caller wires; the
     manager simply asks via ``sealed_exists()`` and overrides the flag.

Required fix #3 (persistence ordering: user turn → LLM → assistant turn)
is a server.py invariant on the WS handler — enforced by the existing
ordering at lines 2714-2715 (history.append for user, then assistant).
This module documents but does not enforce it; the regression test lives
in tests/test_server_locked_state.py.

Recommendation folded in: ``anthropic_client`` is nulled on lock so the
in-memory API key is cleared (cheap to rebuild after unlock).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

log = logging.getLogger("jarvis.idle_lock")

# ---------------------------------------------------------------------------
# Defaults (vault keys override at runtime)
# ---------------------------------------------------------------------------

DEFAULT_IDLE_LOCK_S: float = 900.0           # 15 minutes
DEFAULT_IDLE_LOCK_S_SEALED: float = 120.0    # 2 minutes when sealed convos exist
DEFAULT_TICK_S: float = 60.0                 # check interval
WANDERED_WS_MULTIPLIER: float = 2.0          # 2x threshold when WS still connected
CLOCK_JUMP_THRESHOLD_S: float = 300.0        # idle_for > IDLE_LOCK_S + this -> anomaly
WS_CLOSE_CODE: int = 4423                    # custom 4xxx for vault-locked

# Strict bounds on the configured threshold so a bad vault value can't
# disable the protection.
IDLE_LOCK_S_MIN: float = 60.0
IDLE_LOCK_S_MAX: float = 86400.0  # 24 hours


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class IdleLockManager:
    """Stateful activity tracker. Single instance per server process.

    ``touch()`` records the most recent activity timestamp. It MUST be
    called from:
      - vault.unlock() on successful unlock (the initial activity);
      - the vault-locked middleware on every protected request that PASSES
        auth (covers all HTTP traffic);
      - the WS receive loop on every received frame, **text AND binary**
        (per security-advisor required fix #1 — binary covers audio chunks
        during long Aria turns; without it, wandered-WS misfires mid-utterance).
    """

    def __init__(self):
        self._last_activity_ts: float = time.time()

    def touch(self) -> None:
        """Record activity. Cheap; safe to call at high frequency."""
        self._last_activity_ts = time.time()

    @property
    def last_activity_ts(self) -> float:
        return self._last_activity_ts

    def idle_for(self) -> float:
        return time.time() - self._last_activity_ts

    def evaluate(
        self,
        idle_lock_s: float,
        ws_count: int,
        sealed_exists: bool = False,
        sealed_idle_lock_s: Optional[float] = None,
    ) -> tuple[bool, dict]:
        """Decide whether to lock now. Returns ``(should_lock, audit_extras)``.

        - ``ws_count == 0``: pure idle. Threshold = ``idle_lock_s``.
        - ``ws_count > 0``: wandered-WS. Threshold = ``idle_lock_s * 2``.
        - When ``sealed_exists`` and ``sealed_idle_lock_s`` is set, use the
          stricter sealed threshold instead (advisor required fix #6 + Q1).

        Audit extras (advisor required fix #4):
          - ``had_ws: bool`` — single behavioral classifier.
          - ``clock_jump: true`` only when set (required fix #5).
        Never ``idle`` vs ``wandered_ws`` — that distinction is a habit
        side-channel and offers no forensic value the timestamp doesn't.
        """
        # Sealed-aware threshold (advisor required fix #6 / Q1).
        if sealed_exists and sealed_idle_lock_s is not None:
            base = min(idle_lock_s, sealed_idle_lock_s)
        else:
            base = idle_lock_s
        threshold = base if ws_count == 0 else base * WANDERED_WS_MULTIPLIER

        idle = self.idle_for()
        if idle <= threshold:
            return False, {}

        audit: dict = {"had_ws": ws_count > 0}
        if idle > base + CLOCK_JUMP_THRESHOLD_S:
            audit["clock_jump"] = True
        return True, audit


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

# Type aliases for the injected callables — keep server.py wiring explicit.
GetConfig = Callable[[], tuple[float, bool, bool, Optional[float]]]
GetWsCount = Callable[[], int]
LockAction = Callable[[dict], Awaitable[None]]


def clamp_idle_lock_s(raw: float) -> float:
    """Clamp the vault-supplied threshold to the safe range."""
    if raw < IDLE_LOCK_S_MIN:
        return IDLE_LOCK_S_MIN
    if raw > IDLE_LOCK_S_MAX:
        return IDLE_LOCK_S_MAX
    return raw


async def run_idle_lock_loop(
    manager: IdleLockManager,
    get_config: GetConfig,
    get_ws_count: GetWsCount,
    on_lock: LockAction,
    tick_s: float = DEFAULT_TICK_S,
) -> None:
    """Background task. Polls activity + threshold every ``tick_s`` seconds.

    Injection contract — what each callable returns:
      - ``get_config()`` -> ``(idle_lock_s, enabled, sealed_exists, sealed_idle_lock_s)``.
        - ``enabled``: False when ``IDLE_LOCK_DISABLED`` is true AND no sealed
          conversations exist. If sealed_exists, server.py must force enabled
          to True (advisor required fix #6).
      - ``get_ws_count()`` -> current active WS client count.
      - ``on_lock(audit_extras)`` -> close WS, lock vault, append audit line,
        wipe in-memory caches. Async. Receives the extras dict from
        ``IdleLockManager.evaluate``.

    The loop catches Exception so a transient failure doesn't kill the task.
    asyncio.CancelledError propagates (graceful shutdown).
    """
    while True:
        try:
            await asyncio.sleep(tick_s)
        except asyncio.CancelledError:
            raise

        try:
            idle_lock_s, enabled, sealed_exists, sealed_idle_lock_s = get_config()
            if not enabled:
                continue
            idle_lock_s = clamp_idle_lock_s(idle_lock_s)
            ws_count = get_ws_count()
            should_lock, extras = manager.evaluate(
                idle_lock_s, ws_count, sealed_exists, sealed_idle_lock_s,
            )
            if should_lock:
                log.info("idle_lock: locking (had_ws=%s, clock_jump=%s)",
                         extras.get("had_ws"), extras.get("clock_jump", False))
                await on_lock(extras)
                # Reset so we don't immediately fire again on the next tick.
                manager.touch()
        except Exception:
            # NEVER let the loop die. Log and continue.
            log.exception("idle_lock: loop iteration failed (suppressed)")
