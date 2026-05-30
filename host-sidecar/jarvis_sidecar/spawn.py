"""
SpawnManager — runs ``claude -p`` on the host for the /spawn endpoint.

Spec: docs/superpowers/specs/2026-05-29-sidecar-spawn-design.md.
Security: ``claude --dangerously-skip-permissions`` means the workdir
+ argv allowlist + concurrency caps + audit log are the only structural
guards on what claude does. See SECURITY.md for the threat model.

Key invariants enforced here:
- Argv-only; never shell. Prompt flows via stdin pipe.
- Process group isolation: ``start_new_session=True`` + ``killpg`` on
  the group reaps MCP-server orphans on timeout / DELETE / output_overrun.
- Soft output cap (truncate-and-mark) + hard output cap (kill group).
- Rolling per-minute spawn budget on top of the concurrency cap.
- Prompt bytes NEVER reach any log line, including exception paths.
- Audit log appends one JSON line per spawn / reject / finish /
  timeout / killed / delete, with session_id + caller_fingerprint.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import os
import shutil
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from . import config

log = logging.getLogger("jarvis_sidecar.spawn")

# Exact argv used to invoke claude. No user-controlled flags. Matches
# JARVIS-side claude_runner.py for grep parity.
CLAUDE_ARGV: tuple[str, ...] = (
    "claude",
    "-p",
    "--output-format", "text",
    "--dangerously-skip-permissions",
)


class SpawnError(RuntimeError):
    """Raised on validation / admission failure. Caller maps to HTTP."""


def caller_fingerprint(token: str) -> str:
    """First 8 hex chars of sha256(token) — distinguishes callers in audit log."""
    if not token:
        return "no-token"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def _iso(ts: float) -> str:
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _audit_write(rec: dict) -> None:
    """Append one JSON line. Swallows all errors — must never expose prompt."""
    try:
        path = config.audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except Exception:
        # Never let an audit-log failure leak via exception text into our logs.
        log.error("audit log write failed (suppressed)")


def claude_available() -> bool:
    """For /health.spawn_ready — true iff `claude` resolves on PATH."""
    return shutil.which("claude") is not None


@dataclass
class SpawnSession:
    session_id: str
    workdir: str
    prompt_bytes: int
    caller_fp: str
    started_at: float
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    # status: running | finished | failed | timeout | killed
    status: str = "running"
    output: bytearray = field(default_factory=bytearray)
    output_truncated: bool = False
    # kill_reason: output_overrun | timeout | caller | None
    kill_reason: Optional[str] = None
    _proc: Optional[asyncio.subprocess.Process] = None
    _watcher: Optional[asyncio.Task] = None

    def to_response(self) -> dict:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "output": bytes(self.output).decode("utf-8", errors="replace"),
            "output_truncated": self.output_truncated,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "kill_reason": self.kill_reason,
        }


class SpawnManager:
    def __init__(self):
        self._sessions: dict[str, SpawnSession] = {}
        self._recent: deque[float] = deque()  # spawn-create timestamps (rolling 60s)
        self._lock = asyncio.Lock()

    # ----- admission helpers -------------------------------------------------

    def _prune_rate_window(self) -> None:
        cutoff = time.time() - 60.0
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()

    def _active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.status == "running")

    def _evict_expired(self) -> None:
        now = time.time()
        ttl = config.SESSION_TTL_S
        expired = [
            sid for sid, s in self._sessions.items()
            if s.finished_at is not None and (now - s.finished_at) > ttl
        ]
        for sid in expired:
            del self._sessions[sid]

    # ----- public API --------------------------------------------------------

    async def spawn(
        self,
        prompt: str,
        workdir: str,
        timeout_s: float,
        caller_fp: str,
    ) -> SpawnSession:
        """Admit + spawn. Validation of workdir/prompt/agent is the caller's job.
        Raises SpawnError on admission failure (caller maps to HTTP 429/500)."""
        prompt_bytes_b = prompt.encode("utf-8")
        async with self._lock:
            self._evict_expired()
            self._prune_rate_window()
            if self._active_count() >= config.SPAWN_MAX_CONCURRENT:
                raise SpawnError(
                    f"too many concurrent sessions ({config.SPAWN_MAX_CONCURRENT})"
                )
            if len(self._recent) >= config.SPAWN_MAX_PER_MINUTE:
                raise SpawnError(
                    f"rate cap ({config.SPAWN_MAX_PER_MINUTE}/min) exceeded"
                )
            self._recent.append(time.time())
            sid = uuid.uuid4().hex
            session = SpawnSession(
                session_id=sid,
                workdir=workdir,
                prompt_bytes=len(prompt_bytes_b),
                caller_fp=caller_fp,
                started_at=time.time(),
            )
            self._sessions[sid] = session

        # Spawn outside the lock so a slow exec doesn't block admission.
        try:
            proc = await asyncio.create_subprocess_exec(
                *CLAUDE_ARGV,
                cwd=workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as e:
            # Spawn itself failed. Mark and log — but DO NOT include the
            # exception text if it might contain prompt content (it shouldn't,
            # but defense in depth: log the class name only).
            session.status = "failed"
            session.finished_at = time.time()
            session.exit_code = -1
            _audit_write({
                "ts": _iso(time.time()),
                "verb": "spawn_failed",
                "session_id": sid,
                "caller_fingerprint": caller_fp,
                "workdir": workdir,
                "prompt_bytes": session.prompt_bytes,
                "error_class": type(e).__name__,
            })
            raise SpawnError(f"failed to spawn claude ({type(e).__name__})")

        session._proc = proc

        _audit_write({
            "ts": _iso(session.started_at),
            "verb": "spawn",
            "session_id": sid,
            "caller_fingerprint": caller_fp,
            "workdir": workdir,
            "prompt_bytes": session.prompt_bytes,
        })

        # Watcher owns the prompt bytes — never expose them through the manager.
        session._watcher = asyncio.create_task(
            self._watch(session, prompt_bytes_b, timeout_s)
        )
        return session

    def get(self, session_id: str) -> Optional[SpawnSession]:
        self._evict_expired()
        return self._sessions.get(session_id)

    async def kill(self, session_id: str, caller_fp: str) -> Optional[SpawnSession]:
        """Caller-initiated kill. Returns the session (or None if unknown)."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.status == "running":
            session.kill_reason = "caller"
            await self._kill_group(session)
            session.status = "killed"
            _audit_write({
                "ts": _iso(time.time()),
                "verb": "killed",
                "session_id": session_id,
                "caller_fingerprint": caller_fp,
                "by_caller": True,
            })
        return session

    # ----- internals ---------------------------------------------------------

    async def _watch(
        self,
        session: SpawnSession,
        prompt_bytes_b: bytes,
        timeout_s: float,
    ) -> None:
        proc = session._proc
        assert proc is not None

        async def feed_stdin() -> None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(prompt_bytes_b)
                    await proc.stdin.drain()
                    proc.stdin.close()
            except Exception:
                # Log the class only — never the exception text (may carry
                # buffered prompt bytes in some asyncio implementations).
                log.warning(
                    "spawn %s: stdin feed failed (class=%s)",
                    session.session_id, "stdin-feed-error",
                )

        async def read_stdout() -> None:
            assert proc.stdout is not None
            soft = config.OUTPUT_MAX_BYTES
            hard = config.OUTPUT_HARD_CAP_BYTES
            total_received = 0
            while True:
                try:
                    chunk = await proc.stdout.read(8192)
                except Exception:
                    return
                if not chunk:
                    return
                total_received += len(chunk)
                if total_received > hard:
                    # Fill what fits up to soft cap, then kill the group.
                    space = max(0, soft - len(session.output))
                    if space:
                        session.output.extend(chunk[:space])
                    session.output_truncated = True
                    session.kill_reason = "output_overrun"
                    await self._kill_group(session)
                    session.status = "killed"
                    return
                if total_received > soft:
                    space = max(0, soft - len(session.output))
                    if space:
                        session.output.extend(chunk[:space])
                    session.output_truncated = True
                    # Continue draining so the proc doesn't block on pipe back-pressure.
                else:
                    session.output.extend(chunk)

        feed_task = asyncio.create_task(feed_stdin())
        read_task = asyncio.create_task(read_stdout())

        try:
            await asyncio.wait_for(
                asyncio.gather(read_task, feed_task, return_exceptions=True),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            session.kill_reason = "timeout"
            await self._kill_group(session)
            session.status = "timeout"
            # Cancel + drain the read/feed tasks so the cancelled gather's
            # CancelledError is retrieved (silences asyncio's stderr warning).
            for t in (read_task, feed_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(read_task, feed_task, return_exceptions=True)

        # Reap.
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        session.exit_code = proc.returncode
        session.finished_at = time.time()
        if session.status == "running":
            session.status = "finished" if proc.returncode == 0 else "failed"

        duration_ms = int((session.finished_at - session.started_at) * 1000)
        rec = {
            "ts": _iso(session.finished_at),
            "verb": session.status,
            "session_id": session.session_id,
            "caller_fingerprint": session.caller_fp,
            "workdir": session.workdir,
            "prompt_bytes": session.prompt_bytes,
            "status": session.status,
            "exit_code": session.exit_code,
            "duration_ms": duration_ms,
        }
        if session.kill_reason:
            rec["kill_reason"] = session.kill_reason
        if session.output_truncated:
            rec["output_truncated"] = True
        _audit_write(rec)

    async def _kill_group(self, session: SpawnSession) -> None:
        """SIGTERM then SIGKILL the entire process group. Reaps MCP-server orphans."""
        proc = session._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            # Already gone.
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return
        await asyncio.sleep(0.5)
        if proc.returncode is None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                # Try direct PID kill as last resort.
                try:
                    proc.kill()
                except Exception:
                    pass
