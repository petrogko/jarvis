"""
Hermetic /stt tests — mocks subprocess so neither ffmpeg nor whisper-cli
need to exist on the test runner.
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


async def test_stt_happy_path(client, monkeypatch):
    c, _ = client
    captured = []

    async def fake_exec(*argv, **kwargs):
        captured.append(list(argv))
        if argv[0].endswith("ffmpeg"):
            # ffmpeg call — touch the output WAV.
            # ffmpeg argv: [ffmpeg, "-y", "-i", upload, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path]
            wav_path = pathlib.Path(argv[-1])
            wav_path.write_bytes(b"FAKE-WAV-PCM")
        else:
            # whisper-cli call. -of <prefix> means it writes <prefix>.txt.
            of_idx = argv.index("-of")
            prefix = pathlib.Path(argv[of_idx + 1])
            (prefix.with_suffix(".txt")).write_text("hello world\n", encoding="utf-8")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    audio_blob = b"WEBM-OPUS-FAKE-BLOB"
    files = {"audio": ("clip.webm", BytesIO(audio_blob), "audio/webm")}
    r = c.post("/stt", files=files, headers=HEADERS)

    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "hello world"
    assert body["duration_ms"] >= 0

    # Two subprocess calls: ffmpeg, then whisper-cli.
    assert len(captured) == 2
    assert "ffmpeg" in captured[0][0]
    assert "-ar" in captured[0] and "16000" in captured[0]
    assert "-ac" in captured[0] and "1" in captured[0]
    assert captured[1][0].endswith("whisper-cli")
    assert "-nt" in captured[1] and "-np" in captured[1]
    assert "-otxt" in captured[1]


def test_stt_requires_token(client):
    c, _ = client
    files = {"audio": ("clip.webm", BytesIO(b"x"), "audio/webm")}
    r = c.post("/stt", files=files)
    assert r.status_code == 401


async def test_stt_whisper_nonzero_exit_returns_500(client, monkeypatch):
    c, _ = client

    async def fake_exec(*argv, **kwargs):
        if argv[0].endswith("ffmpeg"):
            wav_path = pathlib.Path(argv[-1])
            wav_path.write_bytes(b"x")
            return _FakeProc(returncode=0)
        return _FakeProc(returncode=2)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    files = {"audio": ("clip.webm", BytesIO(b"x"), "audio/webm")}
    r = c.post("/stt", files=files, headers=HEADERS)
    assert r.status_code == 500


def test_stt_rejects_oversized_upload(client):
    """5 MiB cap defends against resource exhaustion."""
    c, _ = client
    big = b"x" * (5 * 1024 * 1024 + 1)
    files = {"audio": ("clip.webm", BytesIO(big), "audio/webm")}
    r = c.post("/stt", files=files, headers=HEADERS)
    assert r.status_code == 413
