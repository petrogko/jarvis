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

# Detect whether the installed pysqlcipher3/libsqlcipher was compiled with FTS5.
# System apt packages on some distros omit -DSQLITE_ENABLE_FTS5.
def _pysqlcipher_has_fts5() -> bool:
    try:
        from pysqlcipher3 import dbapi2 as _sc
        _conn = _sc.connect(":memory:")
        _conn.execute("PRAGMA key='probe'")
        _conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        _conn.close()
        return True
    except Exception:
        return False

_HAS_FTS5 = _pysqlcipher_has_fts5()
requires_fts5 = pytest.mark.skipif(
    not _HAS_FTS5,
    reason="libsqlcipher on this system was not compiled with FTS5 support",
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import vault


@pytest.fixture(autouse=True)
def _vault_teardown():
    """Ensure vault is locked between tests so module-level _session doesn't leak."""
    yield
    vault.lock()


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


def test_derive_key_returns_bytearray():
    """Regression guard: _derive_key must return a mutable bytearray, not bytes.

    The zeroing path in _zero_bytearray requires a bytearray; if a future
    refactor accidentally returns bytes, ctypes.from_buffer will raise a
    TypeError instead of silently leaving key material in memory.
    """
    result = vault._derive_key("x", b"0" * 16)
    assert isinstance(result, bytearray)


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


@requires_fts5
def test_memory_module_uses_vault_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")

    vault.bootstrap("pp")
    vault.unlock("pp")

    import memory
    # _get_conn() returns the live vault session's memory_conn.
    conn = memory._get_conn()
    assert conn is vault.session().memory_conn

    vault.lock()
    with pytest.raises(vault.VaultLockedError):
        memory._get_conn()

    # Re-unlock and verify create_schema is idempotent and runs cleanly.
    vault.unlock("pp")
    memory.create_schema()   # idempotent if exists
    memory.create_schema()   # idempotent — safe to call twice


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
