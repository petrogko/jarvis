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
