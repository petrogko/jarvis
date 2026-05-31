"""
Hermetic /tts tests — mocks asyncio.create_subprocess_exec so we don't
need a real `say` binary on CI Linux.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = "test-token-12345"
HEADERS = {"X-SIDECAR-Token": TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    (tmp_path / "token").write_text(TOKEN, encoding="utf-8")
    for mod in list(sys.modules):
        if mod.startswith("jarvis_sidecar"):
            del sys.modules[mod]
    from jarvis_sidecar.app import create_app
    return TestClient(create_app()), tmp_path


class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


async def test_tts_argv_safety(client, monkeypatch):
    """Voice + text pass through argv after `--`. No shell."""
    c, tmp = client
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        # Simulate `say` writing the m4a file. argv index 4 is the output path.
        outpath = pathlib.Path(argv[4])
        outpath.write_bytes(b"AAC-PAYLOAD-FAKE")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    r = c.post("/tts", json={"text": "hello world", "voice": "Alex"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.content == b"AAC-PAYLOAD-FAKE"
    assert r.headers["content-type"] == "audio/m4a"
    argv = captured["argv"]
    assert argv[0] == "/usr/bin/say"
    assert "-v" in argv and "Alex" in argv
    assert "-o" in argv
    assert "--file-format=m4af" in argv and "--data-format=aac" in argv
    assert "--" in argv
    assert argv[-1] == "hello world"  # text passes via argv, not shell


def test_tts_requires_token(client):
    c, _ = client
    r = c.post("/tts", json={"text": "x", "voice": "Alex"})
    assert r.status_code == 401


async def test_tts_say_exit_nonzero_returns_500(client, monkeypatch):
    c, _ = client

    async def fake_exec(*argv, **kwargs):
        return _FakeProc(returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    r = c.post("/tts", json={"text": "hello", "voice": "Alex"}, headers=HEADERS)
    assert r.status_code == 500


def test_tts_rejects_empty_text(client):
    c, _ = client
    r = c.post("/tts", json={"text": "", "voice": "Alex"}, headers=HEADERS)
    assert r.status_code == 400


def _patch_say(monkeypatch, payload=b"AAC-SAY"):
    async def fake_say(*argv, **kwargs):
        pathlib.Path(argv[4]).write_bytes(payload)
        return _FakeProc(returncode=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_say)


async def test_tts_engine_piper_when_available(client, monkeypatch):
    c, _ = client
    from jarvis_sidecar import app as app_mod

    monkeypatch.setattr(app_mod, "piper_is_available", lambda: True)
    async def fake_synth(text, voice):
        return b"RIFFWAVE-PIPER"
    monkeypatch.setattr(app_mod, "piper_synthesize", fake_synth)

    r = c.post("/tts", json={"text": "hello", "voice": "en_GB-alan-medium", "engine": "piper"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.content == b"RIFFWAVE-PIPER"
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.headers.get("X-TTS-Engine-Used") == "piper"


async def test_tts_engine_piper_falls_back_to_say_when_unavailable(client, monkeypatch):
    c, _ = client
    from jarvis_sidecar import app as app_mod

    monkeypatch.setattr(app_mod, "piper_is_available", lambda: False)
    _patch_say(monkeypatch, b"AAC-SAY-FALLBACK")

    r = c.post("/tts", json={"text": "hello", "voice": "Alex", "engine": "piper"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.content == b"AAC-SAY-FALLBACK"
    assert r.headers.get("X-TTS-Engine-Used") == "say"


async def test_tts_engine_piper_bad_voice_returns_400(client, monkeypatch):
    c, _ = client
    from jarvis_sidecar import app as app_mod

    monkeypatch.setattr(app_mod, "piper_is_available", lambda: True)
    # Real synthesize runs and rejects the bad voice → PiperError "invalid voice" → 400.
    r = c.post("/tts", json={"text": "hi", "voice": "--evil", "engine": "piper"}, headers=HEADERS)
    assert r.status_code == 400


async def test_tts_default_engine_is_say(client, monkeypatch):
    c, _ = client
    _patch_say(monkeypatch, b"AAC-SAY")
    r = c.post("/tts", json={"text": "hello", "voice": "Alex"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.headers.get("X-TTS-Engine-Used") == "say"
