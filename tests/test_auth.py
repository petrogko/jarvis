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


def _reload_auth(tmp_token_path: pathlib.Path | None = None):
    """Reload auth.py, optionally redirecting the token file."""
    if "auth" in sys.modules:
        del sys.modules["auth"]
    import auth as _auth  # noqa: WPS433
    if tmp_token_path is not None:
        _auth._TOKEN_PATH = tmp_token_path  # type: ignore[attr-defined]
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


def test_load_or_create_token_persists(tmp_path):
    token_file = tmp_path / ".local_token"
    auth = _reload_auth(token_file)
    t1 = auth.load_or_create_token()
    assert len(t1) >= 32
    assert token_file.exists()
    # Idempotent — second call returns the same token
    t2 = auth.load_or_create_token()
    assert t1 == t2
    # And the file is mode 0600
    mode = token_file.stat().st_mode & 0o777
    assert mode in (0o600, 0o400), f"expected 0600, got {oct(mode)}"


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
