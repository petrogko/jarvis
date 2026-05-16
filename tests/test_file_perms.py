"""
Tests for file_perms.harden_secrets_at_startup.

Verifies that:
- A 0644 .env / token / db is tightened to 0600.
- A 0600 file is left alone (no spurious chmod call).
- A 0755 data/ dir is tightened to 0700.
- Missing files don't raise.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reload_file_perms(env_path, data_dir):
    if "file_perms" in sys.modules:
        del sys.modules["file_perms"]
    import file_perms as fp  # noqa: WPS433
    fp.ENV_FILE = env_path  # type: ignore[attr-defined]
    fp.DATA_DIR = data_dir  # type: ignore[attr-defined]
    return fp


def test_tightens_world_readable_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=secret")
    os.chmod(env, 0o644)
    data = tmp_path / "data"
    data.mkdir()
    os.chmod(data, 0o755)
    fp = _reload_file_perms(env, data)
    fp.harden_secrets_at_startup()
    assert env.stat().st_mode & 0o777 == 0o600
    assert data.stat().st_mode & 0o777 == 0o700


def test_leaves_already_tight_alone(tmp_path):
    env = tmp_path / ".env"
    env.write_text("x")
    os.chmod(env, 0o600)
    data = tmp_path / "data"
    data.mkdir()
    os.chmod(data, 0o700)
    fp = _reload_file_perms(env, data)
    fp.harden_secrets_at_startup()
    assert env.stat().st_mode & 0o777 == 0o600
    assert data.stat().st_mode & 0o777 == 0o700


def test_tightens_db_files(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "memory.db"
    db.write_text("(sqlite payload)")
    os.chmod(db, 0o644)
    shm = data / "memory.db-shm"
    shm.write_text("")
    os.chmod(shm, 0o644)
    fp = _reload_file_perms(tmp_path / ".env-nope", data)
    fp.harden_secrets_at_startup()
    assert db.stat().st_mode & 0o777 == 0o600
    assert shm.stat().st_mode & 0o777 == 0o600


def test_missing_files_do_not_raise(tmp_path):
    fp = _reload_file_perms(tmp_path / "nope.env", tmp_path / "nope-data")
    fp.harden_secrets_at_startup()  # must not raise


def test_tightens_local_token(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    token = data / ".local_token"
    token.write_text("t" * 40)
    os.chmod(token, 0o644)
    fp = _reload_file_perms(tmp_path / ".env-nope", data)
    fp.harden_secrets_at_startup()
    assert token.stat().st_mode & 0o777 == 0o600
