# UI-Config + Encrypted Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `.env` + plaintext `data/jarvis.db` with two SQLCipher-backed databases (`data/secrets.db`, `data/jarvis.db`) unlocked by a user passphrase via UI lock-screen, with safe one-shot migration of existing plaintext data.

**Architecture:** A new `vault.py` module owns all SQLCipher I/O and is the only place that touches the master key. `auth.py` and `memory.py` route through `vault.session()`. `server.py` gains a `vault_locked` middleware that returns `423` on `/api/*` and `/ws/*` while the vault is locked, plus four new auth endpoints (`/api/auth/state|bootstrap|unlock|lock`). Migration runs atomically inside `vault.bootstrap()` and the next two unlocks. Frontend `main.ts` gains a boot-state state machine that gates the existing voice UI behind the lock-screen.

**Tech Stack:** Python 3.11, `pysqlcipher3==1.2.0` (bundled libsqlcipher 4.x), `argon2-cffi==23.1.0`, FastAPI middleware, Vite + TypeScript. Tests in `pytest`.

**Parallelism markers:** each task carries `[SEQUENTIAL after T_n]` or `[PARALLEL-OK]`. Subagent-driven execution should fan out independent tasks.

**Persona gates per CLAUDE.md routing:**
- `security-advisor` already reviewed the spec; no per-task re-review unless `SECURITY.md`/`auth.py` is touched (tripwire hook fires automatically).
- `code-reviewer` invoked before every commit on tasks that touch ≥30 LOC or any security-sensitive file (`vault.py`, `auth.py`, `server.py`, `memory.py`).
- `test-runner` invoked before any "ready to merge" / PR creation claim.

---

## Task 1: Add cryptography dependencies

**Files:**
- Modify: `requirements.txt`
- Modify: `Dockerfile` (apt install + remove playwright filter)

**Parallelism:** Blocks all later tasks (vault.py needs the libs). `[SEQUENTIAL]`

- [ ] **Step 1: Append the two pinned deps to `requirements.txt`**

Append after the existing `pyyaml==6.0.2` line:

```
# Vault (P1+P2) — pinned for crypto auditability; bump deliberately.
# pysqlcipher3 bundles libsqlcipher 4.x — see SECURITY.md.
pysqlcipher3==1.2.0
argon2-cffi==23.1.0
```

- [ ] **Step 2: Add libsqlcipher build deps to `Dockerfile`**

Replace the existing `apt-get install` line in `Dockerfile`:

```dockerfile
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        libsqlcipher-dev \
        libsqlcipher0 \
        build-essential \
        python3-dev \
 && rm -rf /var/lib/apt/lists/*
```

Note: `build-essential` + `python3-dev` are needed for the `pysqlcipher3` wheel to build against libsqlcipher. They add ~150 MiB to the image; acceptable.

- [ ] **Step 3: Rebuild and verify the image builds**

Run: `docker compose -p jarvis build 2>&1 | tail -n 5`
Expected: `jarvis-backend:local  Built` and no `error: ` lines.

- [ ] **Step 4: Verify the new packages import**

Run:
```bash
docker run --rm jarvis-backend:local python -c "import pysqlcipher3.dbapi2 as sq; import argon2; print('ok', sq.sqlite_version, argon2.__version__)"
```
Expected: line starting with `ok` followed by versions (sqlite_version ≥ 3.42, argon2 23.1.0).

- [ ] **Step 5: Commit**

```bash
git add requirements.txt Dockerfile
git commit -m "feat(vault): add pysqlcipher3 + argon2-cffi deps"
```

---

## Task 2: vault.py skeleton + bootstrap

**Files:**
- Create: `vault.py`
- Create: `tests/test_vault.py`

**Parallelism:** `[SEQUENTIAL after T1]`. Blocks T3–T8.

- [ ] **Step 1: Write the failing test for module surface**

Create `tests/test_vault.py`:

```python
"""
Hermetic tests for vault.py — the SQLCipher session manager.

Each test uses a tmp_path so no global state leaks. Tests cover:
- bootstrap + unlock + lock + re-unlock round-trip
- wrong passphrase preserves files and raises VaultLockedError
- bootstrap is atomic (re-bootstrap fails with VaultExistsError)
- key material is zeroed on lock
- Argon2 parameters match the spec
- migration: legacy plaintext memory.db + .env -> encrypted store, originals -> .bak
- migration auto-cleanup on second successful unlock
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import vault


def test_module_surface_exists():
    """Public surface required by spec §4."""
    assert callable(vault.bootstrap)
    assert callable(vault.unlock)
    assert callable(vault.lock)
    assert callable(vault.session)
    assert callable(vault.is_initialized)
    assert issubclass(vault.VaultLockedError, Exception)
    assert issubclass(vault.VaultExistsError, Exception)


def test_argon2_parameters_match_spec():
    """Spec §6: memory_cost=256 MiB, time_cost=3, parallelism=4, output=32 bytes."""
    assert vault.ARGON2_MEMORY_KIB == 256 * 1024
    assert vault.ARGON2_TIME_COST == 3
    assert vault.ARGON2_PARALLELISM == 4
    assert vault.KEY_LENGTH_BYTES == 32
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_vault.py::test_module_surface_exists -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'vault'`.

- [ ] **Step 3: Create `vault.py` with the module surface and Argon2 constants**

```python
"""
vault.py — SQLCipher session manager for JARVIS.

This module is the ONLY place that touches the master key. Everything
else routes through `vault.session()` to get a live decrypted connection.

Layout (spec §3):
  data/secrets.db   — SQLCipher; API keys, auth token, UI preferences
  data/jarvis.db    — SQLCipher; conversation memory + tasks
  data/audit.jsonl  — plaintext, append-only (forensic preservation; spec §11)
  data/kdf.salt     — public, 16 bytes random, mode 0644

The master key is derived from a user passphrase via Argon2id
(spec §6 parameters). Held in memory as a bytearray; zeroed via
ctypes.memset on lock().

Thread safety: the module is intentionally NOT thread-safe.
Callers must serialize unlock/lock at the FastAPI app layer.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import pathlib
import secrets
from dataclasses import dataclass
from typing import Optional

from argon2.low_level import Type as Argon2Type, hash_secret_raw
from pysqlcipher3 import dbapi2 as sqlcipher

log = logging.getLogger("jarvis.vault")

# Argon2id parameters — spec §6. Locked here; changing them is a
# vault-format migration.
ARGON2_MEMORY_KIB = 256 * 1024  # 256 MiB
ARGON2_TIME_COST = 3
ARGON2_PARALLELISM = 4
KEY_LENGTH_BYTES = 32  # SQLCipher master key length
SALT_LENGTH_BYTES = 16


# Paths (overridable for tests).
_BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = _BASE_DIR / "data"
SALT_PATH = DATA_DIR / "kdf.salt"
SECRETS_DB_PATH = DATA_DIR / "secrets.db"
MEMORY_DB_PATH = DATA_DIR / "jarvis.db"


class VaultLockedError(Exception):
    """Wrong passphrase, or session not yet unlocked."""


class VaultExistsError(Exception):
    """Bootstrap attempted on an already-initialized vault."""


@dataclass
class VaultSession:
    """Live unlocked session — holds open SQLCipher connections.

    Held in module-private `_session`; never returned with its key
    visible. Closed by `vault.lock()`.
    """
    secrets_conn: sqlcipher.Connection
    memory_conn: sqlcipher.Connection
    _key: bytearray  # zeroed on lock()


_session: Optional[VaultSession] = None


def is_initialized() -> bool:
    """True iff `data/kdf.salt` exists (the bootstrap marker)."""
    return SALT_PATH.exists()


def session() -> Optional[VaultSession]:
    """Current unlocked session, or None if locked."""
    return _session


def lock() -> None:
    """Close connections and zero the in-memory key. Idempotent."""
    global _session
    if _session is None:
        return
    try:
        _session.secrets_conn.close()
    except sqlcipher.Error:
        pass
    try:
        _session.memory_conn.close()
    except sqlcipher.Error:
        pass
    # Deterministically zero the key buffer.
    _zero_bytearray(_session._key)
    _session = None


def _zero_bytearray(b: bytearray) -> None:
    """Overwrite a bytearray's memory with zeros via ctypes.memset.

    Python's GC does not guarantee immediate zeroing; this gives us
    deterministic erasure (spec §6).
    """
    if not b:
        return
    addr = (ctypes.c_char * len(b)).from_buffer(b)
    ctypes.memset(addr, 0, len(b))


def _derive_key(passphrase: str, salt: bytes) -> bytearray:
    """Argon2id KDF with locked parameters. Returns a mutable bytearray."""
    raw = hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_KIB,
        parallelism=ARGON2_PARALLELISM,
        hash_len=KEY_LENGTH_BYTES,
        type=Argon2Type.ID,
    )
    return bytearray(raw)


def bootstrap(passphrase: str) -> None:
    """First-run setup: create salt + empty encrypted DBs.

    Atomic guard: O_CREAT | O_EXCL on salt creation. If the vault
    already exists, raises VaultExistsError without modifying anything.
    """
    raise NotImplementedError  # Step 5/6 below


def unlock(passphrase: str) -> VaultSession:
    """Open both DBs with the derived key. Raises VaultLockedError on bad passphrase."""
    raise NotImplementedError  # Step 7 below
```

- [ ] **Step 4: Run module-surface test to verify it now passes**

Run: `pytest tests/test_vault.py::test_module_surface_exists tests/test_vault.py::test_argon2_parameters_match_spec -v`
Expected: 2 PASSED.

- [ ] **Step 5: Write the failing bootstrap test**

Append to `tests/test_vault.py`:

```python
def test_bootstrap_creates_salt_and_dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")

    assert not vault.is_initialized()
    vault.bootstrap("correct horse battery staple")
    assert vault.is_initialized()
    assert (tmp_path / "kdf.salt").exists()
    assert (tmp_path / "kdf.salt").stat().st_size == vault.SALT_LENGTH_BYTES
    # Salt must be world-readable per spec §6 ("public by design").
    assert (tmp_path / "kdf.salt").stat().st_mode & 0o077 == 0o044
    assert (tmp_path / "secrets.db").exists()
    assert (tmp_path / "jarvis.db").exists()
    # Bootstrap closes the session after creating; caller must unlock().
    assert vault.session() is None


def test_bootstrap_is_atomic(tmp_path, monkeypatch):
    """Re-bootstrap on an initialized vault raises VaultExistsError without
    touching the existing files."""
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")

    vault.bootstrap("first")
    salt_before = (tmp_path / "kdf.salt").read_bytes()
    with pytest.raises(vault.VaultExistsError):
        vault.bootstrap("second")
    assert (tmp_path / "kdf.salt").read_bytes() == salt_before, "salt must not be overwritten"
```

- [ ] **Step 6: Run to verify failures**

Run: `pytest tests/test_vault.py -v -k bootstrap`
Expected: FAIL with `NotImplementedError`.

- [ ] **Step 7: Implement `bootstrap()` and `unlock()`**

Replace the two `raise NotImplementedError` stubs in `vault.py`:

```python
def bootstrap(passphrase: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic salt creation — O_CREAT | O_EXCL is the mutex (spec §5).
    salt = secrets.token_bytes(SALT_LENGTH_BYTES)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(str(SALT_PATH), flags, 0o644)
    except OSError as e:
        if e.errno == errno.EEXIST:
            raise VaultExistsError(f"Vault already exists at {SALT_PATH}")
        raise
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)

    # Derive key and create the two encrypted DBs.
    key = _derive_key(passphrase, salt)
    try:
        for path in (SECRETS_DB_PATH, MEMORY_DB_PATH):
            conn = sqlcipher.connect(str(path))
            try:
                _apply_key(conn, key)
                # Initial schema — minimal; full schema in T3/T6.
                if path == SECRETS_DB_PATH:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS secrets ("
                        "key TEXT PRIMARY KEY, "
                        "value TEXT NOT NULL, "
                        "updated_at TEXT NOT NULL)"
                    )
                conn.commit()
            finally:
                conn.close()
            # Tighten permissions on the encrypted DB files.
            os.chmod(path, 0o600)
    finally:
        _zero_bytearray(key)


def unlock(passphrase: str) -> VaultSession:
    global _session
    if _session is not None:
        return _session  # idempotent — already unlocked
    if not is_initialized():
        raise VaultLockedError("Vault not initialized; call bootstrap() first")

    salt = SALT_PATH.read_bytes()
    key = _derive_key(passphrase, salt)
    try:
        secrets_conn = sqlcipher.connect(str(SECRETS_DB_PATH))
        memory_conn = sqlcipher.connect(str(MEMORY_DB_PATH))
        _apply_key(secrets_conn, key)
        _apply_key(memory_conn, key)
        # Validate by executing a trivial query — raises on wrong key.
        secrets_conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        memory_conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlcipher.DatabaseError as e:
        # SQLCipher raises this on wrong key. Zero and re-raise as ours.
        _zero_bytearray(key)
        try:
            secrets_conn.close()
        except Exception:
            pass
        try:
            memory_conn.close()
        except Exception:
            pass
        raise VaultLockedError("Wrong passphrase") from e

    _session = VaultSession(secrets_conn=secrets_conn, memory_conn=memory_conn, _key=key)
    return _session


def _apply_key(conn: sqlcipher.Connection, key: bytearray) -> None:
    """Set the SQLCipher key and pin compatibility to 4.x (spec §6)."""
    # Pass the key as a hex literal — pysqlcipher3's safest path.
    hex_key = bytes(key).hex()
    conn.execute(f"PRAGMA key = \"x'{hex_key}'\"")
    conn.execute("PRAGMA cipher_compatibility = 4")
```

- [ ] **Step 8: Run all bootstrap tests to verify they pass**

Run: `pytest tests/test_vault.py -v`
Expected: 4 PASSED.

- [ ] **Step 9: Add wrong-passphrase + lock-zeroes tests**

Append to `tests/test_vault.py`:

```python
def test_unlock_wrong_passphrase_raises_and_leaves_files_intact(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")

    vault.bootstrap("right-passphrase")
    secrets_bytes = (tmp_path / "secrets.db").read_bytes()
    salt_bytes = (tmp_path / "kdf.salt").read_bytes()

    with pytest.raises(vault.VaultLockedError):
        vault.unlock("wrong-passphrase")

    # Files MUST be byte-identical after a failed unlock (spec §3 failure modes table).
    assert (tmp_path / "secrets.db").read_bytes() == secrets_bytes
    assert (tmp_path / "kdf.salt").read_bytes() == salt_bytes
    assert vault.session() is None


def test_unlock_then_lock_zeroes_key(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")

    vault.bootstrap("pp")
    sess = vault.unlock("pp")
    # Capture a reference to the underlying bytearray before lock zeroes it.
    key_ref = sess._key
    assert bytes(key_ref) != bytes(len(key_ref))  # nonzero before lock
    vault.lock()
    assert bytes(key_ref) == bytes(len(key_ref)), "key bytes must be zeroed on lock"
    assert vault.session() is None
```

- [ ] **Step 10: Run all vault tests**

Run: `pytest tests/test_vault.py -v`
Expected: 6 PASSED.

- [ ] **Step 11: Commit**

```bash
git add vault.py tests/test_vault.py
git commit -m "feat(vault): core bootstrap/unlock/lock with Argon2id + SQLCipher"
```

---

## Task 3: vault.settings helper (secrets get/set)

**Files:**
- Modify: `vault.py` (add `VaultSession.settings` namespace)
- Modify: `tests/test_vault.py`

**Parallelism:** `[SEQUENTIAL after T2]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_vault.py`:

```python
def test_settings_get_set_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")

    vault.bootstrap("pp")
    sess = vault.unlock("pp")

    sess.settings.set("ANTHROPIC_API_KEY", "sk-ant-test")
    assert sess.settings.get("ANTHROPIC_API_KEY") == "sk-ant-test"
    assert sess.settings.get("MISSING") is None
    assert sess.settings.get("MISSING", default="fallback") == "fallback"

    # Persistence across lock/unlock.
    vault.lock()
    sess2 = vault.unlock("pp")
    assert sess2.settings.get("ANTHROPIC_API_KEY") == "sk-ant-test"

    # Listing whitelisted keys (used by /api/settings/status).
    sess2.settings.set("FISH_API_KEY", "fish-x")
    listing = sess2.settings.list_all()
    assert listing == {"ANTHROPIC_API_KEY": "sk-ant-test", "FISH_API_KEY": "fish-x"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_vault.py::test_settings_get_set_roundtrip -v`
Expected: FAIL with `AttributeError: 'VaultSession' object has no attribute 'settings'`.

- [ ] **Step 3: Add the settings namespace**

In `vault.py`, add this class above `VaultSession`:

```python
class _SettingsNamespace:
    """Typed accessor for the `secrets` table on `secrets.db`."""

    def __init__(self, conn: sqlcipher.Connection):
        self._conn = conn

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM secrets WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        from datetime import datetime
        self._conn.execute(
            "INSERT INTO secrets(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, datetime.utcnow().isoformat() + "Z"),
        )
        self._conn.commit()

    def delete(self, key: str) -> None:
        self._conn.execute("DELETE FROM secrets WHERE key = ?", (key,))
        self._conn.commit()

    def list_all(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM secrets").fetchall()
        return {k: v for k, v in rows}
```

Update `VaultSession` to expose it:

```python
@dataclass
class VaultSession:
    secrets_conn: sqlcipher.Connection
    memory_conn: sqlcipher.Connection
    _key: bytearray

    @property
    def settings(self) -> "_SettingsNamespace":
        return _SettingsNamespace(self.secrets_conn)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_vault.py -v`
Expected: 7 PASSED.

- [ ] **Step 5: Commit**

```bash
git add vault.py tests/test_vault.py
git commit -m "feat(vault): settings get/set/list namespace on secrets DB"
```

---

## Task 4: vault migration helper

**Files:**
- Modify: `vault.py` (add `migrate_from_legacy`)
- Modify: `tests/test_vault.py`

**Parallelism:** `[SEQUENTIAL after T3]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_vault.py`:

```python
def _make_legacy_state(tmp_path: pathlib.Path) -> None:
    """Build a plaintext memory.db (== jarvis.db at the legacy path) + .env."""
    # Legacy plaintext memory DB.
    legacy = tmp_path / "jarvis.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO memory(content) VALUES('I prefer dry martinis')")
    conn.execute("INSERT INTO memory(content) VALUES('Birthday is April 4')")
    conn.commit()
    conn.close()
    # Legacy .env at the bootstrap mount path.
    (tmp_path / ".env.bootstrap").write_text(
        "ANTHROPIC_API_KEY=sk-ant-legacy\nFISH_API_KEY=fish-legacy\n",
        encoding="utf-8",
    )


def test_migrate_imports_memory_and_env(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")
    monkeypatch.setattr(vault, "LEGACY_ENV_PATH", tmp_path / ".env.bootstrap")

    _make_legacy_state(tmp_path)
    # Bootstrap MOVES the legacy plaintext DB to a .bak first so it can
    # write the encrypted one at the same path.
    vault.bootstrap("pp")
    sess = vault.unlock("pp")
    vault.migrate_from_legacy(sess)

    # Secrets imported.
    assert sess.settings.get("ANTHROPIC_API_KEY") == "sk-ant-legacy"
    assert sess.settings.get("FISH_API_KEY") == "fish-legacy"

    # Memory imported — verify row count matches.
    cnt = sess.memory_conn.execute("SELECT count(*) FROM memory").fetchone()[0]
    assert cnt == 2

    # Backup files created.
    assert (tmp_path / "jarvis.db.pre-encrypt.bak").exists()
    assert (tmp_path / ".env.bootstrap.pre-encrypt.bak").exists()


def test_migrate_auto_cleanup_on_second_unlock(tmp_path, monkeypatch):
    """Spec §8 step 7: after the second successful unlock, .bak files are deleted."""
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")
    monkeypatch.setattr(vault, "LEGACY_ENV_PATH", tmp_path / ".env.bootstrap")

    _make_legacy_state(tmp_path)
    vault.bootstrap("pp")
    sess = vault.unlock("pp")
    vault.migrate_from_legacy(sess)
    assert (tmp_path / "jarvis.db.pre-encrypt.bak").exists()

    # Second unlock — should auto-delete backups.
    vault.lock()
    vault.unlock("pp")
    # The auto-cleanup happens inside unlock().
    assert not (tmp_path / "jarvis.db.pre-encrypt.bak").exists()
    assert not (tmp_path / ".env.bootstrap.pre-encrypt.bak").exists()
```

- [ ] **Step 2: Run to verify failures**

Run: `pytest tests/test_vault.py -v -k migrate`
Expected: 2 FAILED (`AttributeError` on `LEGACY_ENV_PATH` / `migrate_from_legacy`).

- [ ] **Step 3: Implement migration + auto-cleanup**

Add to `vault.py` after the path constants:

```python
LEGACY_ENV_PATH = _BASE_DIR / ".env.bootstrap"
MIGRATION_FLAG_KEY = "_vault_migration_unlock_count"
ALLOWED_LEGACY_ENV_KEYS = {
    "ANTHROPIC_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID",
    "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS",
}
```

Modify `bootstrap()` — before opening the encrypted DBs, move any legacy plaintext DB out of the way:

```python
def bootstrap(passphrase: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # If a plaintext jarvis.db is sitting at the target path, move it
    # to a .bak so the SQLCipher file can be created at the same path.
    if MEMORY_DB_PATH.exists():
        bak = MEMORY_DB_PATH.with_suffix(MEMORY_DB_PATH.suffix + ".pre-encrypt.bak")
        if not bak.exists():
            os.rename(MEMORY_DB_PATH, bak)
            os.chmod(bak, 0o600)

    # ... existing salt creation + DB creation code ...
```

Add the migration function and update `unlock()` to call the auto-cleanup:

```python
def migrate_from_legacy(sess: VaultSession) -> None:
    """One-shot import of legacy plaintext data. Idempotent — skips if
    already migrated. Called once after bootstrap completes."""

    # Memory import — read from .bak (bootstrap moved it).
    legacy_db_bak = MEMORY_DB_PATH.with_suffix(MEMORY_DB_PATH.suffix + ".pre-encrypt.bak")
    if legacy_db_bak.exists():
        already_imported = sess.memory_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory'"
        ).fetchone()
        if not already_imported:
            src = sqlcipher.connect(f"file:{legacy_db_bak}?mode=ro", uri=True)  # plaintext SQLite is readable
            # Copy schema + rows table by table.
            tables = src.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for name, ddl in tables:
                sess.memory_conn.execute(ddl)
                rows = src.execute(f"SELECT * FROM {name}").fetchall()
                if rows:
                    placeholders = ",".join("?" for _ in rows[0])
                    sess.memory_conn.executemany(
                        f"INSERT INTO {name} VALUES ({placeholders})", rows
                    )
            sess.memory_conn.commit()
            src.close()

    # .env import — read from the optional bind-mount path.
    if LEGACY_ENV_PATH.exists():
        env_bak = LEGACY_ENV_PATH.with_suffix(LEGACY_ENV_PATH.suffix + ".pre-encrypt.bak")
        if not env_bak.exists():
            for line in LEGACY_ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k in ALLOWED_LEGACY_ENV_KEYS and v:
                    sess.settings.set(k, v)
            os.rename(LEGACY_ENV_PATH, env_bak)
            os.chmod(env_bak, 0o600)

    # Mark migration done; start the unlock counter at 0 for cleanup tracking.
    if sess.settings.get(MIGRATION_FLAG_KEY) is None:
        sess.settings.set(MIGRATION_FLAG_KEY, "0")
```

Update `unlock()` — at the end (before returning the session), bump the migration unlock counter and auto-clean:

```python
    # Post-unlock: auto-clean migration backups after the SECOND unlock.
    # (Spec §8 step 7.)
    sess = _session
    count_str = sess.settings.get(MIGRATION_FLAG_KEY)
    if count_str is not None:
        count = int(count_str) + 1
        sess.settings.set(MIGRATION_FLAG_KEY, str(count))
        if count >= 2:
            _purge_migration_backups()
    return sess


def _purge_migration_backups() -> None:
    for path in (
        MEMORY_DB_PATH.with_suffix(MEMORY_DB_PATH.suffix + ".pre-encrypt.bak"),
        LEGACY_ENV_PATH.with_suffix(LEGACY_ENV_PATH.suffix + ".pre-encrypt.bak"),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
```

- [ ] **Step 4: Run all vault tests**

Run: `pytest tests/test_vault.py -v`
Expected: 9 PASSED.

- [ ] **Step 5: Commit**

```bash
git add vault.py tests/test_vault.py
git commit -m "feat(vault): legacy plaintext migration + auto-cleanup after 2nd unlock"
```

---

## Task 5: auth.py — token in vault + _PUBLIC_PATHS expansion

**Files:**
- Modify: `auth.py`
- Modify: `tests/test_auth.py`

**Parallelism:** `[SEQUENTIAL after T3]`. `[PARALLEL-OK with T6]`.

- [ ] **Step 1: Read existing `auth.py`**

Run: `cat auth.py`
Note: existing `_PUBLIC_PATHS` set, `load_or_create_token()` function, `LocalTokenAuthMiddleware` class.

- [ ] **Step 2: Write failing tests for the new behavior**

Append to `tests/test_auth.py`:

```python
def test_public_paths_include_vault_auth_endpoints():
    """Spec §5 + security-advisor required fix #4."""
    import auth
    for path in ("/api/auth/state", "/api/auth/bootstrap", "/api/auth/unlock", "/api/auth/lock"):
        assert path in auth._PUBLIC_PATHS, f"{path} must be in _PUBLIC_PATHS"


def test_load_or_create_token_uses_vault_when_unlocked(tmp_path, monkeypatch):
    import auth
    import vault

    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")
    monkeypatch.setattr(vault, "LEGACY_ENV_PATH", tmp_path / ".env.bootstrap")

    vault.bootstrap("pp")
    sess = vault.unlock("pp")

    # First call: generates and stores in vault.
    t1 = auth.load_or_create_token()
    assert t1
    assert sess.settings.get("AUTH_TOKEN") == t1
    # Second call: idempotent — returns the same token.
    t2 = auth.load_or_create_token()
    assert t1 == t2

    vault.lock()
```

- [ ] **Step 3: Run to verify failures**

Run: `pytest tests/test_auth.py -v -k "public_paths or vault"`
Expected: FAILs.

- [ ] **Step 4: Update `auth.py`**

In `_PUBLIC_PATHS`, add the four endpoints:

```python
_PUBLIC_PATHS = frozenset({
    "/api/health",
    "/api/auth/state",
    "/api/auth/bootstrap",
    "/api/auth/unlock",
    "/api/auth/lock",
    # ... whatever was already there ...
})
```

Replace `load_or_create_token()`:

```python
def load_or_create_token() -> str:
    """Read auth token from the vault. Generates and stores one if absent.

    REQUIRES the vault to be unlocked. Returns "" if locked — callers
    must check for this and prompt unlock before authenticating.
    """
    import secrets as _secrets
    import vault

    sess = vault.session()
    if sess is None:
        return ""
    existing = sess.settings.get("AUTH_TOKEN")
    if existing:
        return existing
    new_token = _secrets.token_urlsafe(32)
    sess.settings.set("AUTH_TOKEN", new_token)
    return new_token
```

Remove any reference to `data/.local_token` file path from auth.py — token no longer touches disk outside the vault.

- [ ] **Step 5: Run all auth tests**

Run: `pytest tests/test_auth.py -v`
Expected: All PASS, including the two new tests.

- [ ] **Step 6: Code-reviewer persona pass on auth.py changes**

This touches a membrane file (`auth.py`) — invoke `code-reviewer` persona via the Agent tool with subagent_type matching the persona definition. Pass the diff for review. Apply any must-fix findings before commit.

- [ ] **Step 7: Commit**

```bash
git add auth.py tests/test_auth.py
git commit -m "feat(auth): token storage moves to vault; _PUBLIC_PATHS extended"
```

---

## Task 6: server.py — vault-locked middleware + /api/auth/* endpoints

**Files:**
- Modify: `server.py`
- Create: `tests/test_server_locked_state.py`

**Parallelism:** `[SEQUENTIAL after T3]`. `[PARALLEL-OK with T5]`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_server_locked_state.py`:

```python
"""Verify the vault-locked middleware on server.py."""

from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    import vault
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")
    monkeypatch.setattr(vault, "LEGACY_ENV_PATH", tmp_path / ".env.bootstrap")
    yield vault
    vault.lock()


def _client():
    from server import app
    return TestClient(app)


def test_health_reachable_while_locked(isolated_vault):
    c = _client()
    r = c.get("/api/health")
    assert r.status_code == 200


def test_state_reports_uninitialized_then_initialized(isolated_vault):
    c = _client()
    r = c.get("/api/auth/state")
    assert r.json() == {"initialized": False, "locked": True}
    isolated_vault.bootstrap("pp")
    r = c.get("/api/auth/state")
    assert r.json() == {"initialized": True, "locked": True}


def test_bootstrap_then_unlock_flow(isolated_vault):
    c = _client()
    r = c.post("/api/auth/bootstrap", json={"passphrase": "pp"})
    assert r.status_code == 200
    # Bootstrap leaves the vault locked; client must unlock explicitly.
    r = c.post("/api/auth/unlock", json={"passphrase": "pp"})
    assert r.status_code == 200


def test_bootstrap_idempotency_returns_409(isolated_vault):
    c = _client()
    c.post("/api/auth/bootstrap", json={"passphrase": "pp"})
    r = c.post("/api/auth/bootstrap", json={"passphrase": "pp2"})
    assert r.status_code == 409


def test_unlock_wrong_passphrase_returns_401(isolated_vault):
    c = _client()
    c.post("/api/auth/bootstrap", json={"passphrase": "pp"})
    r = c.post("/api/auth/unlock", json={"passphrase": "wrong"})
    assert r.status_code == 401


def test_unlock_rate_limit_returns_429(isolated_vault):
    """Spec §5: rate limit MUST fire before KDF. Second attempt within 2s -> 429."""
    c = _client()
    c.post("/api/auth/bootstrap", json={"passphrase": "pp"})
    r1 = c.post("/api/auth/unlock", json={"passphrase": "wrong"})
    assert r1.status_code == 401
    r2 = c.post("/api/auth/unlock", json={"passphrase": "pp"})
    assert r2.status_code == 429, f"expected 429 from rate limit, got {r2.status_code}"


def test_protected_endpoint_returns_423_while_locked(isolated_vault):
    """Spec §5: all /api/* except auth + health return 423 while locked."""
    c = _client()
    isolated_vault.bootstrap("pp")
    # /api/settings/status is one of the existing endpoints; while locked it must 423.
    r = c.get("/api/settings/status")
    assert r.status_code == 423


def test_protected_endpoint_reachable_after_unlock(isolated_vault):
    c = _client()
    isolated_vault.bootstrap("pp")
    c.post("/api/auth/unlock", json={"passphrase": "pp"})
    r = c.get("/api/settings/status")
    # Auth token is in the vault; client doesn't have it for this test. Expect
    # either 200 (if /api/settings/status is in _PUBLIC_PATHS) or 401 — NOT 423.
    assert r.status_code != 423
```

- [ ] **Step 2: Run to verify failures**

Run: `pytest tests/test_server_locked_state.py -v`
Expected: most FAIL (endpoints don't exist yet, middleware doesn't exist).

- [ ] **Step 3: Add Pydantic models for the auth endpoints near the top of `server.py`**

```python
class _PassphraseBody(BaseModel):
    passphrase: str
```

(`BaseModel` is already imported.)

- [ ] **Step 4: Add the vault-locked middleware in `server.py` after the existing `LocalTokenAuthMiddleware` is added**

```python
# Vault-locked middleware. MUST be installed AFTER LocalTokenAuthMiddleware
# so auth runs first (per spec §5 middleware order). Both middlewares allow
# the auth + health endpoints through.
import vault as _vault_mod

_VAULT_PUBLIC_PATHS = frozenset({
    "/api/health",
    "/api/auth/state",
    "/api/auth/bootstrap",
    "/api/auth/unlock",
})


@app.middleware("http")
async def vault_locked_middleware(request, call_next):
    if request.url.path in _VAULT_PUBLIC_PATHS:
        return await call_next(request)
    if _vault_mod.session() is None:
        from starlette.responses import JSONResponse
        return JSONResponse({"detail": "vault locked"}, status_code=423)
    return await call_next(request)
```

- [ ] **Step 5: Add the four `/api/auth/*` endpoints + the rate-limit guard**

```python
_LAST_UNLOCK_ATTEMPT = {"t": 0.0}
_UNLOCK_MIN_INTERVAL_S = 2.0


@app.get("/api/auth/state")
async def api_auth_state():
    return {
        "initialized": _vault_mod.is_initialized(),
        "locked": _vault_mod.session() is None,
    }


@app.post("/api/auth/bootstrap")
async def api_auth_bootstrap(body: _PassphraseBody):
    from fastapi import HTTPException
    try:
        _vault_mod.bootstrap(body.passphrase)
    except _vault_mod.VaultExistsError:
        raise HTTPException(status_code=409, detail="vault already initialized")
    return {"ok": True}


@app.post("/api/auth/unlock")
async def api_auth_unlock(body: _PassphraseBody):
    """Rate limit FIRES BEFORE the KDF (security-advisor required fix #1)."""
    from fastapi import HTTPException
    now = time.monotonic()
    if now - _LAST_UNLOCK_ATTEMPT["t"] < _UNLOCK_MIN_INTERVAL_S:
        raise HTTPException(status_code=429, detail="too many unlock attempts")
    _LAST_UNLOCK_ATTEMPT["t"] = now
    try:
        sess = _vault_mod.unlock(body.passphrase)
    except _vault_mod.VaultLockedError:
        raise HTTPException(status_code=401, detail="wrong passphrase")
    # Best-effort one-shot migration after the first unlock.
    try:
        _vault_mod.migrate_from_legacy(sess)
    except Exception as e:
        log.exception("migration failed: %s", e)
    return {"ok": True}


@app.post("/api/auth/lock")
async def api_auth_lock():
    _vault_mod.lock()
    return {"ok": True}
```

(`time` and `log` already imported in `server.py`.)

- [ ] **Step 6: Run the new tests**

Run: `pytest tests/test_server_locked_state.py -v`
Expected: all 8 PASSED.

- [ ] **Step 7: Code-reviewer persona pass**

Touches `server.py` security-sensitive paths — invoke `code-reviewer` persona on the diff. Apply must-fix items.

- [ ] **Step 8: Commit**

```bash
git add server.py tests/test_server_locked_state.py
git commit -m "feat(server): vault-locked middleware + /api/auth/* endpoints"
```

---

## Task 7: server.py — route /api/settings/* through vault

**Files:**
- Modify: `server.py`

**Parallelism:** `[SEQUENTIAL after T6]`.

- [ ] **Step 1: Identify all `os.getenv` call sites for the migrated keys**

Run: `grep -nE 'os\.getenv\("(ANTHROPIC|FISH)_' server.py`
Expected: at minimum the 4 call sites enumerated in spec §8 (`server.py:74`, `:75–77`, `:2635`, `:2647`). Document all hits.

- [ ] **Step 2: Add a helper at the top of `server.py`**

```python
def _vault_get(key: str, default: str = "") -> str:
    """Read a config value from the unlocked vault.

    Returns the default if the vault is locked (the caller should have
    been blocked by the vault-locked middleware, but this is defensive).
    """
    sess = _vault_mod.session()
    if sess is None:
        return default
    return sess.settings.get(key, default=default) or default
```

- [ ] **Step 3: Replace each `os.getenv("ANTHROPIC_API_KEY", "")` etc. with `_vault_get("ANTHROPIC_API_KEY")`**

Locations (verify line numbers may have drifted):
- `server.py:74` — `ANTHROPIC_API_KEY = os.getenv(...)` → DELETE the module-level assignment entirely; replace with a function call at each use site. The module-level was a `.env`-era pattern.
- `server.py:75-77` — same treatment for `FISH_API_KEY`, `FISH_VOICE_ID`.
- `server.py:2635` — `key = body.key_value or os.getenv("ANTHROPIC_API_KEY", "")` → `key = body.key_value or _vault_get("ANTHROPIC_API_KEY")`.
- `server.py:2647` — same for fish.

Find every `anthropic.AsyncAnthropic()` / `anthropic.AsyncClient()` constructor — they read the env var by default. Make them explicit: `anthropic.AsyncAnthropic(api_key=_vault_get("ANTHROPIC_API_KEY"))`.

- [ ] **Step 4: Update `/api/settings/keys` to write to the vault, not `.env`**

Replace the body of `api_settings_keys` at `server.py:2625`:

```python
@app.post("/api/settings/keys")
async def api_settings_keys(body: KeyUpdate):
    allowed = {"ANTHROPIC_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID",
               "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS"}
    if body.key_name not in allowed:
        raise HTTPException(status_code=400, detail="key not allowed")
    sess = _vault_mod.session()
    if sess is None:
        raise HTTPException(status_code=423, detail="vault locked")
    sess.settings.set(body.key_name, body.key_value)
    return {"ok": True}
```

- [ ] **Step 5: Delete `_read_env` and `_write_env_key`**

Remove both functions from `server.py`. Update `/api/settings/status` and `/api/settings/preferences` to use `sess.settings.list_all()` / `sess.settings.get(...)` instead.

- [ ] **Step 6: Sweep test — assert no stale call sites remain**

Add to `tests/test_server_locked_state.py`:

```python
def test_no_stale_env_calls_remain():
    """Spec §8: after migration ships, no module outside vault.py may read
    ANTHROPIC_API_KEY / FISH_* from os.environ directly."""
    import re
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    pattern = re.compile(r'os\.getenv\(\s*[\'"](?:ANTHROPIC|FISH)_')
    offenders = []
    for py in repo_root.glob("*.py"):
        if py.name in ("vault.py", "conftest.py"):
            continue
        if pattern.search(py.read_text()):
            offenders.append(py.name)
    assert not offenders, f"these files still call os.getenv directly: {offenders}"
```

- [ ] **Step 7: Run the full server test suite**

Run: `pytest tests/test_server_locked_state.py tests/test_auth.py tests/test_vault.py -v`
Expected: all PASS.

- [ ] **Step 8: Code-reviewer pass**

This is the biggest server.py change in the plan; mandatory `code-reviewer` invocation per CLAUDE.md routing (≥30 LOC + security-sensitive).

- [ ] **Step 9: Commit**

```bash
git add server.py tests/test_server_locked_state.py
git commit -m "feat(server): /api/settings/* and LLM/TTS clients read from vault"
```

---

## Task 8: memory.py — open via vault.session().memory_conn

**Files:**
- Modify: `memory.py`
- Modify: `tests/test_memory.py` (if it exists; else create)

**Parallelism:** `[SEQUENTIAL after T4]`. `[PARALLEL-OK with T5, T6, T7]`.

- [ ] **Step 1: Read `memory.py`**

Run: `cat memory.py`
Identify the `sqlite3.connect(...)` site and any module-level `_DB_PATH` constant.

- [ ] **Step 2: Replace `sqlite3` connection logic with vault-routed connection**

In `memory.py`, replace any module-level `_conn = sqlite3.connect(...)` or factory function with:

```python
def _get_conn():
    """Return the SQLCipher connection from the unlocked vault.

    Raises VaultLockedError if the vault is locked — callers (which are
    all behind the vault-locked middleware) should never hit this in
    practice.
    """
    import vault
    sess = vault.session()
    if sess is None:
        raise vault.VaultLockedError("memory.py called while vault is locked")
    return sess.memory_conn
```

Every existing call to the old connection is replaced with `_get_conn()` at the call site (each function in `memory.py`). Keep the SQL itself unchanged — SQLCipher is wire-compatible with SQLite.

- [ ] **Step 3: Update or add tests that exercise memory through the vault**

Add to `tests/test_vault.py` (it has the vault fixture pattern already):

```python
def test_memory_module_uses_vault_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")

    vault.bootstrap("pp")
    vault.unlock("pp")

    import memory
    # Pick any existing memory.py public function that touches the DB.
    # (Replace `memory.create_schema()` with whatever exists in the actual module.)
    memory.create_schema()  # idempotent; raises if conn invalid
    conn = memory._get_conn()
    assert conn is vault.session().memory_conn

    vault.lock()
    with pytest.raises(vault.VaultLockedError):
        memory._get_conn()
```

- [ ] **Step 4: Run all memory + vault tests**

Run: `pytest tests/test_vault.py tests/test_memory.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add memory.py tests/test_vault.py
git commit -m "feat(memory): connection routed through vault.session().memory_conn"
```

---

## Task 9: docker-compose changes

**Files:**
- Modify: `docker-compose.yml`

**Parallelism:** `[PARALLEL-OK after T1]`.

- [ ] **Step 1: Remove `env_file` auto-load and document optional bootstrap mount**

In `docker-compose.yml`, replace the existing `env_file: - .env` block with:

```yaml
    # .env is no longer auto-loaded at compose boot — secrets live in the
    # encrypted vault under data/. To bootstrap on first run, optionally
    # mount an .env at /app/.env.bootstrap (read-only) and JARVIS will
    # import its values into the vault on the first successful unlock,
    # then delete the file. Without this mount, enter keys via the UI.
    env_file: []
    # volumes:
    #   - ./.env:/app/.env.bootstrap:ro   # uncomment for first-boot import
```

Verify the existing `volumes:` block (with `./data:/app/data:rw`) remains intact.

- [ ] **Step 2: Update the existing environment block**

Replace with the JARVIS-specific runtime vars that are NOT secrets:

```yaml
    environment:
      DO_NOT_TRACK: "1"
      PIP_DISABLE_PIP_VERSION_CHECK: "1"
      ANTHROPIC_LOG_LEVEL: "warning"
      # Auth token now lives in the vault; do not set JARVIS_TOKEN_REQUIRED.
```

- [ ] **Step 3: Rebuild and verify compose still parses**

Run: `docker compose -p jarvis config 2>&1 | head -n 30`
Expected: no errors; output shows the compiled config.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(docker): remove .env auto-load; vault is the secret store"
```

---

## Task 10: Frontend lock-screen UI

**Files:**
- Modify: `frontend/src/main.ts`
- Create: `frontend/src/lock-screen.ts`
- Create: `frontend/src/lock-screen.css`
- Modify: `frontend/index.html` (add lock-screen container div)

**Parallelism:** `[PARALLEL-OK with T5, T6, T7, T8]` — only needs the API contract from T6 to be settled (which it is, per the spec).

- [ ] **Step 1: Read existing `frontend/src/main.ts`**

Run: `cat frontend/src/main.ts`
Identify the current entry point and the state machine.

- [ ] **Step 2: Create `frontend/src/lock-screen.ts`**

```ts
/**
 * Lock-screen state machine for JARVIS vault unlock.
 *
 * States:
 *   - boot: fetching /api/auth/state
 *   - first-run: user picks a passphrase
 *   - locked: user enters passphrase
 *   - unlocked: hand off to voice UI (resolves the start promise)
 *
 * Renders into an element with id="lock-screen". Removes itself from
 * the DOM on successful unlock.
 */

type AuthState = { initialized: boolean; locked: boolean };

async function fetchState(): Promise<AuthState> {
  const r = await fetch("/api/auth/state");
  return r.json();
}

async function postJson(path: string, body: object): Promise<Response> {
  return fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function renderFirstRun(container: HTMLElement): Promise<void> {
  return new Promise((resolve) => {
    container.innerHTML = `
      <div class="lock-card">
        <h1>Welcome to JARVIS</h1>
        <p>Set a passphrase. JARVIS will require it on every restart.
           <strong>There is no recovery.</strong></p>
        <input type="password" id="lock-pp1" placeholder="passphrase" autofocus />
        <input type="password" id="lock-pp2" placeholder="confirm passphrase" />
        <div class="lock-err" id="lock-err"></div>
        <button id="lock-submit">Set passphrase</button>
      </div>`;
    const btn = container.querySelector("#lock-submit") as HTMLButtonElement;
    btn.onclick = async () => {
      const pp1 = (container.querySelector("#lock-pp1") as HTMLInputElement).value;
      const pp2 = (container.querySelector("#lock-pp2") as HTMLInputElement).value;
      const err = container.querySelector("#lock-err") as HTMLElement;
      if (pp1.length < 8) { err.textContent = "Passphrase too short (min 8)"; return; }
      if (pp1 !== pp2) { err.textContent = "Passphrases do not match"; return; }
      err.textContent = "Bootstrapping…";
      const r = await postJson("/api/auth/bootstrap", { passphrase: pp1 });
      if (!r.ok) { err.textContent = `Error ${r.status}`; return; }
      const r2 = await postJson("/api/auth/unlock", { passphrase: pp1 });
      if (!r2.ok) { err.textContent = `Unlock failed: ${r2.status}`; return; }
      resolve();
    };
  });
}

function renderLocked(container: HTMLElement): Promise<void> {
  return new Promise((resolve) => {
    container.innerHTML = `
      <div class="lock-card">
        <h1>JARVIS</h1>
        <p>Enter your passphrase.</p>
        <input type="password" id="lock-pp" placeholder="passphrase" autofocus />
        <div class="lock-err" id="lock-err"></div>
        <button id="lock-submit">Unlock</button>
      </div>`;
    const btn = container.querySelector("#lock-submit") as HTMLButtonElement;
    const tryUnlock = async () => {
      const pp = (container.querySelector("#lock-pp") as HTMLInputElement).value;
      const err = container.querySelector("#lock-err") as HTMLElement;
      err.textContent = "Unlocking…";
      const r = await postJson("/api/auth/unlock", { passphrase: pp });
      if (r.status === 401) { err.textContent = "Wrong passphrase"; return; }
      if (r.status === 429) { err.textContent = "Slow down — too many attempts"; return; }
      if (!r.ok) { err.textContent = `Error ${r.status}`; return; }
      resolve();
    };
    btn.onclick = tryUnlock;
    (container.querySelector("#lock-pp") as HTMLInputElement).onkeydown = (e) => {
      if (e.key === "Enter") tryUnlock();
    };
  });
}

export async function awaitUnlock(): Promise<void> {
  const container = document.getElementById("lock-screen");
  if (!container) throw new Error("missing #lock-screen container");
  container.style.display = "block";
  const state = await fetchState();
  if (!state.initialized) {
    await renderFirstRun(container);
  } else if (state.locked) {
    await renderLocked(container);
  }
  container.style.display = "none";
  container.innerHTML = "";
}
```

- [ ] **Step 3: Create `frontend/src/lock-screen.css`**

```css
#lock-screen { position: fixed; inset: 0; background: #000; z-index: 9999;
               display: flex; align-items: center; justify-content: center;
               color: #eaeaea; font-family: -apple-system, system-ui, sans-serif; }
.lock-card { width: 320px; padding: 24px; background: #111; border: 1px solid #333;
             border-radius: 8px; }
.lock-card h1 { margin: 0 0 8px; font-weight: 300; }
.lock-card p { color: #888; font-size: 13px; line-height: 1.4; }
.lock-card input { width: 100%; padding: 10px; margin: 6px 0; background: #000;
                   color: #eaeaea; border: 1px solid #333; border-radius: 4px; }
.lock-card button { width: 100%; padding: 10px; margin-top: 10px; background: #2a6;
                    color: #fff; border: 0; border-radius: 4px; cursor: pointer; }
.lock-card button:hover { background: #3b7; }
.lock-err { color: #f55; min-height: 18px; font-size: 13px; margin: 6px 0; }
```

- [ ] **Step 4: Modify `frontend/index.html`**

Add inside `<body>`, BEFORE the existing app container:

```html
<div id="lock-screen" style="display:none"></div>
```

Add inside `<head>`:

```html
<link rel="stylesheet" href="/src/lock-screen.css" />
```

- [ ] **Step 5: Modify `frontend/src/main.ts`**

At the very top of the boot sequence (before the orb / voice loop initializes):

```ts
import { awaitUnlock } from "./lock-screen";

(async () => {
  await awaitUnlock();
  // ... existing main.ts entry code follows unchanged ...
})();
```

- [ ] **Step 6: Build the frontend to verify it compiles**

Run: `cd frontend && npm run build`
Expected: build succeeds with no TypeScript errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lock-screen.ts frontend/src/lock-screen.css frontend/src/main.ts frontend/index.html
git commit -m "feat(frontend): lock-screen UI for vault first-run + unlock"
```

---

## Task 11: SECURITY.md + ARCHITECTURE.md updates

**Files:**
- Modify: `SECURITY.md`
- Modify: `ARCHITECTURE.md`

**Parallelism:** `[PARALLEL-OK after T2]`.

This task fires the membrane tripwire hook (both files are in the membrane). That is expected; security-advisor already reviewed the spec.

- [ ] **Step 1: Read both files**

Run: `cat SECURITY.md ARCHITECTURE.md`

- [ ] **Step 2: Apply the SECURITY.md updates per spec §11**

Concretely:
- Data classification table: update rows for `ANTHROPIC_API_KEY`, `FISH_API_KEY`, `FISH_VOICE_ID`, `auth_token`, memory DB, audit log.
- Add new rows: `data/secrets.db`, `data/kdf.salt`, `data/jarvis.db.pre-encrypt.bak`, `data/.env.bootstrap.pre-encrypt.bak`.
- "What is intentionally NOT defended against" section: add "Passphrase loss equals permanent data loss; no recovery path exists."
- Operator checklist: add "Set a strong passphrase on first run; keep an out-of-band backup. After first unlock, a second unlock clears plaintext migration backups automatically."
- Remove the "API keys live in `.env`" line.
- Remove the "`data/.local_token` file mode 0600" line.

- [ ] **Step 3: Apply the ARCHITECTURE.md updates per spec §11**

- Module map: add `vault.py` row — "SQLCipher session manager; single point of at-rest encryption for secrets and memory".
- Persistence table: replace `.env` row with `data/secrets.db (SQLCipher)`; update token row; mark memory DB as SQLCipher.
- Trust boundaries: update boundary descriptions where "API key from env" appears → "API key from vault".

- [ ] **Step 4: Commit**

```bash
git add SECURITY.md ARCHITECTURE.md
git commit -m "docs(security,arch): document vault as new secrets+memory root of trust"
```

---

## Task 12: Final integration smoke test + acceptance

**Files:**
- Modify: `tests/test_personas_setup.py` (unchanged in behavior — sanity check that the personas system still passes)
- Manual checks

**Parallelism:** `[SEQUENTIAL after all prior tasks]`.

- [ ] **Step 1: Run the full hermetic test suite**

Run: `pytest -q`
Expected: 0 failures. Skipped/excluded tests (integration ones in pyproject.toml) remain skipped.

- [ ] **Step 2: Run pip-audit**

Run: `pip-audit -r requirements.txt --strict`
Expected: `No known vulnerabilities found`.

- [ ] **Step 3: End-to-end Docker smoke**

```bash
# Clean state.
rm -rf data .env  # WARNING: only safe to run in a throwaway env. In a real
                  # branch with existing data, back it up first.
docker compose -p jarvis up -d --wait
curl -s http://127.0.0.1:8340/api/health      # expect: 200 {"status":"online",...}
curl -s http://127.0.0.1:8340/api/auth/state  # expect: 200 {"initialized":false,"locked":true}
# Bootstrap.
curl -sf -X POST http://127.0.0.1:8340/api/auth/bootstrap \
  -H "content-type: application/json" -d '{"passphrase":"test-passphrase"}'
# Unlock.
curl -sf -X POST http://127.0.0.1:8340/api/auth/unlock \
  -H "content-type: application/json" -d '{"passphrase":"test-passphrase"}'
# Locked endpoint now reachable.
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8340/api/settings/status
# Expect: 200 or 401 (depending on auth token presence) — NOT 423.
docker compose -p jarvis down
```

- [ ] **Step 4: test-runner persona pass**

Per CLAUDE.md routing — before any "ready to merge" claim, invoke the `test-runner` persona via Agent tool. It runs `pytest -q` + `pip-audit` and reports exit codes verbatim. Implementer does not interpret; just reads the report.

- [ ] **Step 5: Update `docs/BACKLOG.md`**

Move P1 (and absorbed P2) from "Priority queue" to "Done (recent)". Add a note: "P1+P2 merged into a single rollout."

- [ ] **Step 6: code-reviewer persona on the full branch diff**

```
git diff main...HEAD
```
Invoke `code-reviewer` persona via Agent tool with the diff. Apply must-fix findings before pushing.

- [ ] **Step 7: Push branch + open PR**

```bash
git push -u origin feat/ui-config-encrypted-2026-05
gh pr create --title "feat: vault-encrypted UI-only configuration (P1+P2)" \
  --body "$(cat <<'BODY'
Implements `docs/superpowers/specs/2026-05-24-ui-config-encrypted-storage-design.md`.

## Summary
- Two SQLCipher-encrypted databases (`data/secrets.db`, `data/jarvis.db`) replace `.env` and the previous plaintext memory DB.
- Master key derived from a user passphrase via Argon2id (256 MiB / t=3 / p=4); never persisted to disk.
- UI lock-screen on every container start; rate-limit on unlock fires before KDF.
- Safe one-shot migration of existing plaintext memory + `.env` on first unlock, auto-cleanup on second unlock.
- `auth.py` token storage moves into the vault; `_PUBLIC_PATHS` extended.
- `SECURITY.md` + `ARCHITECTURE.md` updated.

## Test plan
- [x] `pytest -q` — 0 failures
- [x] `pip-audit -r requirements.txt --strict` — clean
- [x] End-to-end Docker smoke (bootstrap → unlock → settings reachable → restart → locked again)
- [x] Wrong passphrase rejected; files untouched
- [x] Migration .bak files auto-deleted on second unlock
- [x] No `os.getenv("ANTHROPIC_API_KEY")` survives outside `vault.py`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

- [ ] **Step 8: Wait for CI green + merge**

```bash
gh pr checks
gh pr merge --squash --delete-branch
```

---

## Self-review against the spec

**Spec coverage:**
- ✅ §1 Goals — covered (T2 vault, T6 server endpoints, T7 settings, T10 UI, T11 docs)
- ✅ §3 Storage layout — T2 creates both DBs; audit log left plaintext per spec
- ✅ §4 Module layout — vault.py (T2), server.py (T6/T7), memory.py (T8), auth.py (T5)
- ✅ §5 Endpoints + middleware ordering + rate-limit-before-KDF + bootstrap atomicity — T6
- ✅ §6 Cryptography (Argon2 params + SQLCipher 4 pin + bytearray zeroing) — T2
- ✅ §7 UI flow — T10
- ✅ §8 Migration sequencing + auto-cleanup + os.getenv audit — T4 + T7
- ✅ §9 Docker compose — T9
- ✅ §10 Tests — distributed across T2/T3/T4/T5/T6/T7/T8/T12
- ✅ §11 SECURITY.md + ARCHITECTURE.md — T11
- ✅ §12 Open questions — resolved in spec; no plan action needed
- ✅ §13 Out-of-scope items — not touched

**Placeholder scan:** No "TBD", no "TODO", no "implement later". Every step has actual code or actual commands.

**Type consistency:** `vault.bootstrap`, `vault.unlock`, `vault.lock`, `vault.session`, `vault.is_initialized`, `vault.migrate_from_legacy`, `vault.VaultLockedError`, `vault.VaultExistsError`, `VaultSession.settings`, `_SettingsNamespace.get/set/delete/list_all`, `vault.DATA_DIR`, `vault.SALT_PATH`, `vault.SECRETS_DB_PATH`, `vault.MEMORY_DB_PATH`, `vault.LEGACY_ENV_PATH`, `vault.ALLOWED_LEGACY_ENV_KEYS`, `vault.MIGRATION_FLAG_KEY`, `vault.ARGON2_*` constants — all defined in T2/T3/T4 and used consistently in T5/T6/T7/T8.

**Persona gates:** code-reviewer invoked at T5 step 6, T6 step 7, T7 step 8, T12 step 6. test-runner invoked at T12 step 4. security-advisor already cleared the spec.

Plan ships as-is.

---

## Parallelism map (for subagent dispatch)

```
T1 (deps) ─┬─ T2 (vault core) ─┬─ T3 (settings) ─┬─ T5 (auth.py)        ─┐
           │                   │                 ├─ T6 (server endpoints) │
           │                   │                 ├─ T7 (server settings)  │
           │                   │                 ├─ T8 (memory.py)        ├── T12 (acceptance)
           │                   │                 ├─ T9 (docker)           │
           │                   │                 ├─ T10 (frontend)        │
           │                   │                 └─ T11 (docs)            │
           │                   └─ T4 (migration) ──────────────────────────┘
           └────────────────────────────────────────────────────────────────
```

T5, T6, T8, T9, T10, T11 can run as parallel subagents once T3 is done. T7 runs after T6. T12 waits for all.
