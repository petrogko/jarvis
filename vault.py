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
    """Set the SQLCipher key and pin compatibility to 4.x (spec §6)."""
    # Pass the key as a hex literal — pysqlcipher3's safest path.
    hex_key = bytes(key).hex()
    conn.execute(f"PRAGMA key = \"x'{hex_key}'\"")
    conn.execute("PRAGMA cipher_compatibility = 4")


def bootstrap(passphrase: str) -> None:
    """First-run setup: create salt + empty encrypted DBs.

    Atomic guard: O_CREAT | O_EXCL on salt creation. If the vault
    already exists, raises VaultExistsError without modifying anything.
    """
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
    return _session
