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
LEGACY_ENV_PATH = _BASE_DIR / ".env.bootstrap"

MIGRATION_FLAG_KEY = "_vault_migration_unlock_count"
ALLOWED_LEGACY_ENV_KEYS = {
    "ANTHROPIC_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID",
    "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS",
}


class VaultLockedError(Exception):
    """Wrong passphrase, or session not yet unlocked."""


class VaultExistsError(Exception):
    """Bootstrap attempted on an already-initialized vault."""


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


@dataclass
class VaultSession:
    """Live unlocked session — holds open SQLCipher connections.

    Held in module-private `_session`; never returned with its key
    visible. Closed by `vault.lock()`.
    """
    secrets_conn: sqlcipher.Connection
    memory_conn: sqlcipher.Connection
    _key: bytearray  # zeroed on lock()

    @property
    def settings(self) -> "_SettingsNamespace":
        return _SettingsNamespace(self.secrets_conn)


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


def _apply_key(conn: sqlcipher.Connection, key: bytearray) -> None:
    """Set the SQLCipher key and pin compatibility to 4.x (spec §6).

    Also pins ``row_factory`` to pysqlcipher3's own Row class so callers
    can rely on ``dict(row)``-style access — sqlite3.Row is incompatible
    with pysqlcipher3 cursors (different C type).
    """
    # Pass the key as a hex literal — pysqlcipher3's safest path.
    hex_key = bytes(key).hex()
    conn.execute(f"PRAGMA key = \"x'{hex_key}'\"")
    conn.execute("PRAGMA cipher_compatibility = 4")
    conn.row_factory = sqlcipher.Row


def bootstrap(passphrase: str) -> None:
    """First-run setup: create salt + empty encrypted DBs.

    Atomic guard: O_CREAT | O_EXCL on salt creation. If the vault
    already exists, raises VaultExistsError without modifying anything.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # If a plaintext jarvis.db is sitting at the target path, move it
    # to a .bak so the SQLCipher file can be created at the same path.
    if MEMORY_DB_PATH.exists():
        bak = MEMORY_DB_PATH.with_suffix(MEMORY_DB_PATH.suffix + ".pre-encrypt.bak")
        if not bak.exists():
            os.rename(MEMORY_DB_PATH, bak)
            os.chmod(bak, 0o600)
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
    """Open both DBs with the derived key. Raises VaultLockedError on bad passphrase."""
    global _session
    if _session is not None:
        return _session  # idempotent — already unlocked
    if not is_initialized():
        raise VaultLockedError("Vault not initialized; call bootstrap() first")

    salt = SALT_PATH.read_bytes()
    key = _derive_key(passphrase, salt)
    secrets_conn = None
    memory_conn = None
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
        if secrets_conn is not None:
            try:
                secrets_conn.close()
            except Exception:
                pass
        if memory_conn is not None:
            try:
                memory_conn.close()
            except Exception:
                pass
        raise VaultLockedError("Wrong passphrase") from e

    _session = VaultSession(secrets_conn=secrets_conn, memory_conn=memory_conn, _key=key)

    # Post-unlock: auto-clean migration backups after the SECOND unlock.
    # (Spec §8 step 7.)
    count_str = _session.settings.get(MIGRATION_FLAG_KEY)
    if count_str is not None:
        count = int(count_str) + 1
        _session.settings.set(MIGRATION_FLAG_KEY, str(count))
        if count >= 1:
            _purge_migration_backups()
    return _session


def _purge_migration_backups() -> None:
    for path in (
        MEMORY_DB_PATH.with_suffix(MEMORY_DB_PATH.suffix + ".pre-encrypt.bak"),
        LEGACY_ENV_PATH.with_suffix(LEGACY_ENV_PATH.suffix + ".pre-encrypt.bak"),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


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
            # Use stdlib sqlite3 to read the legacy plaintext DB (NOT SQLCipher).
            import sqlite3 as _stdlib_sqlite
            src = _stdlib_sqlite.connect(f"file:{legacy_db_bak}?mode=ro", uri=True)
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
