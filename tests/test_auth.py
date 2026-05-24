"""
Tests for the local-token auth middleware and helpers.

Run with:  pytest tests/test_auth.py

The pure-helper tests (loopback detection, constant-time compare,
public-path matcher) run without FastAPI installed. The middleware
tests require fastapi + starlette + httpx.
"""

from __future__ import annotations

import importlib
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reload_auth():
    """Reload auth.py."""
    if "auth" in sys.modules:
        del sys.modules["auth"]
    import auth as _auth  # noqa: WPS433
    return _auth


# ---------------------------------------------------------------------------
# Pure helpers — no FastAPI required
# ---------------------------------------------------------------------------

def test_is_loopback_accepts_localhost_variants():
    auth = _reload_auth()
    assert auth.is_loopback("127.0.0.1")
    assert auth.is_loopback("::1")
    assert auth.is_loopback("localhost")
    assert auth.is_loopback("[::1]")  # bracketed IPv6 form
    assert auth.is_loopback("::1%lo0")  # IPv6 zone id


def test_is_loopback_rejects_lan_and_empty():
    auth = _reload_auth()
    assert not auth.is_loopback("10.0.0.1")
    assert not auth.is_loopback("192.168.1.42")
    assert not auth.is_loopback("8.8.8.8")
    assert not auth.is_loopback("")
    assert not auth.is_loopback(None)


def test_constant_time_equal():
    auth = _reload_auth()
    assert auth.constant_time_equal("abc", "abc")
    assert not auth.constant_time_equal("abc", "abd")
    assert not auth.constant_time_equal("", "abc")
    assert not auth.constant_time_equal("abc", "")


def test_check_token_rejects_empty():
    auth = _reload_auth()
    assert not auth.check_token(None, "expected")
    assert not auth.check_token("", "expected")
    assert auth.check_token("expected", "expected")


def test_load_or_create_token_persists(tmp_path, monkeypatch):
    # Token is now stored in the vault (not a file). Set up an unlocked vault
    # and verify load_or_create_token generates a token, persists it, and is
    # idempotent. (The old data/.local_token file path is gone.)
    import vault

    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")
    monkeypatch.setattr(vault, "LEGACY_ENV_PATH", tmp_path / ".env.bootstrap")

    vault.bootstrap("pp")
    sess = vault.unlock("pp")

    auth = _reload_auth()
    t1 = auth.load_or_create_token()
    assert len(t1) >= 32
    assert sess.settings.get("AUTH_TOKEN") == t1
    # Idempotent — second call returns the same token
    t2 = auth.load_or_create_token()
    assert t1 == t2

    vault.lock()


# ---------------------------------------------------------------------------
# Middleware integration — requires fastapi
# ---------------------------------------------------------------------------

def _skip_if_no_fastapi():
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
        return False
    except ImportError:
        return True


def _build_app(token: str = "test-token", trust_loopback: bool = True):
    from fastapi import FastAPI
    auth = _reload_auth()

    app = FastAPI()
    app.add_middleware(
        auth.LocalTokenAuthMiddleware,
        token=token,
        trust_loopback=trust_loopback,
    )

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/secret")
    def secret():
        return {"secret": True}

    @app.get("/")
    def index():
        return {"frontend": True}

    return app


def test_health_is_public():
    if _skip_if_no_fastapi():
        return
    from fastapi.testclient import TestClient
    app = _build_app()
    r = TestClient(app).get("/api/health")
    assert r.status_code == 200


def test_non_loopback_requires_token():
    if _skip_if_no_fastapi():
        return
    from fastapi.testclient import TestClient
    app = _build_app("the-token", trust_loopback=False)
    client = TestClient(app)
    assert client.get("/api/secret").status_code == 401
    assert client.get("/api/secret", headers={"X-JARVIS-Token": "wrong"}).status_code == 401
    assert client.get("/api/secret", headers={"X-JARVIS-Token": "the-token"}).status_code == 200
    # Query param also accepted
    assert client.get("/api/secret?token=the-token").status_code == 200


def test_options_preflight_bypasses_auth():
    if _skip_if_no_fastapi():
        return
    from fastapi.testclient import TestClient
    app = _build_app("the-token", trust_loopback=False)
    r = TestClient(app).options("/api/secret")
    # Should not be 401. (FastAPI may return 405 if no OPTIONS handler is
    # registered — that's fine; the point is the auth middleware let it
    # through.)
    assert r.status_code != 401


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
