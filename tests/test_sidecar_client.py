"""
Hermetic tests for sidecar_client — mocks httpx.AsyncClient so the tests
don't need a live sidecar.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sidecar_client


@pytest.fixture
def isolated_token(tmp_path, monkeypatch):
    """Bind-mount path for the token. Production lives at
    /host-sidecar-config/token (mounted from the host); tests stub the path."""
    monkeypatch.setattr(sidecar_client, "_TOKEN_PATH", tmp_path / "token")
    (tmp_path / "token").write_text("test-token-xyz", encoding="utf-8")
    yield


class _FakeResp:
    def __init__(self, status_code: int, content: bytes = b"", payload: dict | None = None):
        self.status_code = status_code
        self.content = content
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, headers=None):
        self.calls.append(("GET", url, headers, None))
        return self._responses.pop(0)

    async def post(self, url, headers=None, json=None, files=None, content=None):
        self.calls.append(("POST", url, headers, json or files or content))
        return self._responses.pop(0)


async def test_tts_via_sidecar_happy_path(isolated_token, monkeypatch):
    fake = _FakeClient(_FakeResp(200, content=b"AAC-FAKE"))
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: fake)
    audio = await sidecar_client.tts_via_sidecar("hello", voice="Alex")
    assert audio == b"AAC-FAKE"
    method, url, headers, body = fake.calls[0]
    assert method == "POST"
    assert url.endswith("/tts")
    assert headers["X-SIDECAR-Token"] == "test-token-xyz"
    assert body == {"text": "hello", "voice": "Alex"}


async def test_tts_via_sidecar_returns_none_on_connection_error(isolated_token, monkeypatch):
    class _Boom:
        async def __aenter__(self): raise sidecar_client.httpx.ConnectError("nope")
        async def __aexit__(self, *e): pass
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: _Boom())
    audio = await sidecar_client.tts_via_sidecar("hello", voice="Alex")
    assert audio is None


async def test_stt_via_sidecar_happy_path(isolated_token, monkeypatch):
    fake = _FakeClient(_FakeResp(200, payload={"text": "transcribed", "duration_ms": 350}))
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: fake)
    text = await sidecar_client.stt_via_sidecar(b"AUDIO-BYTES", mime_type="audio/webm")
    assert text == "transcribed"
    method, url, headers, body = fake.calls[0]
    assert method == "POST"
    assert url.endswith("/stt")
    assert headers["X-SIDECAR-Token"] == "test-token-xyz"


async def test_stt_via_sidecar_returns_empty_on_error(isolated_token, monkeypatch):
    fake = _FakeClient(_FakeResp(500, payload={"detail": "whisper-cli exited 2"}))
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: fake)
    text = await sidecar_client.stt_via_sidecar(b"x", mime_type="audio/webm")
    assert text == ""


async def test_sidecar_health(isolated_token, monkeypatch):
    fake = _FakeClient(_FakeResp(200, payload={"status": "ok", "say_available": True}))
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: fake)
    health = await sidecar_client.sidecar_health()
    assert health == {"status": "ok", "say_available": True}


async def test_sidecar_health_none_on_connection_error(isolated_token, monkeypatch):
    class _Boom:
        async def __aenter__(self): raise sidecar_client.httpx.ConnectError("nope")
        async def __aexit__(self, *e): pass
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: _Boom())
    health = await sidecar_client.sidecar_health()
    assert health is None


def test_read_token_missing_returns_empty(monkeypatch):
    """When the bind-mount isn't there (host install, no sidecar), _read_token
    returns "" rather than raising."""
    monkeypatch.setattr(sidecar_client, "_TOKEN_PATH", pathlib.Path("/nonexistent/path"))
    assert sidecar_client._read_token() == ""
