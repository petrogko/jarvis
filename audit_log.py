"""
Append-only JSONL audit log for every executed [ACTION:X] dispatch.

Threat model:
    The action-tag system is the most security-sensitive code path:
    each tag is the LLM's decision to mutate the world (open a
    browser, spawn a subprocess, write a note). The prompt-injection
    defenses in ``untrusted_content`` + the action validators in
    ``server.extract_action`` aim to prevent malicious tags from
    reaching dispatch. This module records what *did* reach dispatch
    so:

      * a successful injection (or a validator gap, like the
        ``javascript:`` bug PR #4 caught) is reconstructable after
        the fact;
      * "why did JARVIS open Chrome to that URL at 3am" is
        answerable from the log alone;
      * blocked actions (validator rejections) are also recorded so
        the rate of injection attempts is observable.

Storage:
    ``data/audit.jsonl`` — newline-delimited JSON, append-only,
    mode 0600. Rotates at 10 MiB → ``audit.jsonl.1.gz`` (current
    becomes .1; existing .1 becomes .2; .5 is dropped).

Format (one object per line):
    {
      "ts": "2026-05-17T03:14:15.926Z",
      "source": "llm-action" | "api-task" | "validator-reject",
      "action": "browse" | "build" | "create_note" | ...,
      "target_summary": "<sanitized, <=200 chars>",
      "user_text_summary": "<sanitized, <=200 chars>",
      "success": true | false,
      "latency_ms": 142,
      "reason": "<optional, present on rejects/failures>"
    }

Sanitization:
    Free-text fields pass through ``untrusted_content.sanitize`` so
    a maliciously crafted target/prompt can't poison the audit log
    itself (control chars, role markers, embedded ACTION tags).

Concurrency:
    Synchronous; serialized by a ``threading.Lock``. File IO at
    action-dispatch frequency (a few per minute) doesn't need
    asyncio. Callable from both sync (``extract_action``) and async
    (``voice_handler``) contexts without coupling.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from untrusted_content import sanitize

log = logging.getLogger("jarvis.audit")

ROOT = Path(__file__).resolve().parent
AUDIT_DIR = ROOT / "data"
AUDIT_PATH = AUDIT_DIR / "audit.jsonl"
ROTATE_AT_BYTES = 10 * 1024 * 1024  # 10 MiB
KEEP_GZIPPED = 5

_MAX_FREETEXT = 200
_FILE_MODE = 0o600

_write_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _rotate_if_needed(audit_path: Path | None = None) -> None:
    """Rotate the audit log when it crosses ``ROTATE_AT_BYTES``.

    Cheap O(1) check (stat); the actual rotation only fires when the
    threshold is crossed. Best-effort: failures here are logged but
    don't block the write that triggered the check.
    """
    path = audit_path if audit_path is not None else AUDIT_PATH
    audit_dir = path.parent
    try:
        if not path.exists():
            return
        if path.stat().st_size < ROTATE_AT_BYTES:
            return
        oldest = audit_dir / f"{path.name}.{KEEP_GZIPPED}.gz"
        if oldest.exists():
            oldest.unlink()
        for i in range(KEEP_GZIPPED - 1, 0, -1):
            src = audit_dir / f"{path.name}.{i}.gz"
            dst = audit_dir / f"{path.name}.{i + 1}.gz"
            if src.exists():
                src.rename(dst)
        new_path = audit_dir / f"{path.name}.1.gz"
        with path.open("rb") as f_in, gzip.open(new_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        path.unlink()
    except OSError as e:
        log.warning("audit log rotation failed: %s", e)


def _open_for_append(path: Path):
    """Ensure dir exists and the file mode is 0600 on first write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    fh = open(path, "a", encoding="utf-8")
    if is_new:
        try:
            os.chmod(path, _FILE_MODE)
        except OSError:
            pass
    return fh


def record(
    *,
    action: str,
    target: str | None = None,
    user_text: str | None = None,
    success: bool,
    latency_ms: int | float | None = None,
    source: str = "llm-action",
    reason: str | None = None,
    path: Path | None = None,
) -> None:
    """Append one audit entry. Never raises; failures are logged.

    ``path`` lets tests redirect the output without touching module state.
    """
    try:
        entry = {
            "ts": _now_iso(),
            "source": source,
            "action": str(action)[:40] if action else "unknown",
            "target_summary": sanitize(target or "", max_len=_MAX_FREETEXT),
            "user_text_summary": sanitize(user_text or "", max_len=_MAX_FREETEXT),
            "success": bool(success),
        }
        if latency_ms is not None:
            entry["latency_ms"] = int(latency_ms)
        if reason:
            entry["reason"] = sanitize(reason, max_len=_MAX_FREETEXT)

        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        out = path if path is not None else AUDIT_PATH

        with _write_lock:
            _rotate_if_needed(out)
            with _open_for_append(out) as fh:
                fh.write(line)
                fh.flush()
    except Exception as e:  # noqa: BLE001 — audit is best-effort
        log.warning("audit_log.record failed: %s", e)
