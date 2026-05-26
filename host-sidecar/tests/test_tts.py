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
