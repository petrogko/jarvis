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


def test_bootstrap_then_unlock_flow(isolated_vault, monkeypatch):
    import server
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
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


def test_unlock_wrong_passphrase_returns_401(isolated_vault, monkeypatch):
    import server
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
    c = _client()
    c.post("/api/auth/bootstrap", json={"passphrase": "pp"})
    r = c.post("/api/auth/unlock", json={"passphrase": "wrong"})
    assert r.status_code == 401


def test_unlock_rate_limit_returns_429(isolated_vault, monkeypatch):
    """Spec §5: rate limit MUST fire before KDF. Second attempt within 2s -> 429."""
    import server
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
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


def test_protected_endpoint_reachable_after_unlock(isolated_vault, monkeypatch):
    import server
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
    c = _client()
    isolated_vault.bootstrap("pp")
    c.post("/api/auth/unlock", json={"passphrase": "pp"})
    r = c.get("/api/settings/status")
    # Auth token is in the vault; client doesn't have it for this test. Expect
    # either 200 (if /api/settings/status is in _PUBLIC_PATHS) or 401 — NOT 423.
    assert r.status_code != 423
