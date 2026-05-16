"""
Startup file-permission hardening.

JARVIS persists three classes of sensitive material on disk:

* ``.env``                — Anthropic / Fish API keys (Secret).
* ``data/.local_token``   — auth token (Secret).
* ``data/*.db``           — SQLite memory + conversation (PII).

Default umask on macOS is usually 0022 → files land at 0644, world-
readable. That is fine for source, fatal for secrets. Anyone else
with an account on the same machine, or any process running as a
different user (Spotlight indexer, Time Machine, third-party
backup), can read them.

This module is called at server startup. It is best-effort: log a
warning if we can't fix something, never raise. On a single-user
Mac the operations are no-ops on a clean install; on a machine with
careless prior copies of these files, it tightens them.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

log = logging.getLogger("jarvis.fileperms")

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
DATA_DIR = ROOT / "data"

# Mode for sensitive files: rw for owner only.
_FILE_SECRET = 0o600
# Mode for the data directory: rwx for owner only.
_DIR_SECRET = 0o700


def _tighten_file(path: Path, *, label: str, target_mode: int = _FILE_SECRET) -> None:
    if not path.exists():
        return
    try:
        current = path.stat().st_mode & 0o777
    except OSError as e:
        log.warning("could not stat %s (%s): %s", label, path, e)
        return
    if current == target_mode:
        return
    # Surface group/other readable explicitly — that's the case that
    # actually matters for secret leakage.
    if current & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        log.warning(
            "%s at %s was mode %s (group/other accessible); tightening to %s",
            label, path, oct(current), oct(target_mode),
        )
    try:
        os.chmod(path, target_mode)
    except OSError as e:
        log.warning("could not chmod %s (%s): %s", label, path, e)


def _tighten_dir(path: Path, *, label: str, target_mode: int = _DIR_SECRET) -> None:
    if not path.exists():
        return
    try:
        current = path.stat().st_mode & 0o777
    except OSError as e:
        log.warning("could not stat %s (%s): %s", label, path, e)
        return
    if current == target_mode:
        return
    if current & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH | stat.S_IXGRP | stat.S_IXOTH):
        log.warning(
            "%s at %s was mode %s (group/other accessible); tightening to %s",
            label, path, oct(current), oct(target_mode),
        )
    try:
        os.chmod(path, target_mode)
    except OSError as e:
        log.warning("could not chmod %s (%s): %s", label, path, e)


def harden_secrets_at_startup() -> None:
    """Tighten file permissions on every sensitive path JARVIS owns.

    Safe to call repeatedly. Never raises. Logs at WARNING level for
    every change made so the operator can investigate provenance.
    """
    _tighten_file(ENV_FILE, label=".env")
    _tighten_dir(DATA_DIR, label="data/")
    _tighten_file(DATA_DIR / ".local_token", label="data/.local_token")
    if DATA_DIR.is_dir():
        for db in DATA_DIR.glob("*.db"):
            _tighten_file(db, label=f"data/{db.name}")
        for shm in DATA_DIR.glob("*.db-shm"):
            _tighten_file(shm, label=f"data/{shm.name}")
        for wal in DATA_DIR.glob("*.db-wal"):
            _tighten_file(wal, label=f"data/{wal.name}")
