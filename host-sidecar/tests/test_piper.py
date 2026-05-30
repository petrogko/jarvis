"""
Hermetic tests for jarvis_sidecar.piper_engine. Mocks subprocess so no
real piper binary / model is needed on CI.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis_sidecar import piper_engine


class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


def test_module_surface():
    assert callable(piper_engine.is_available)
    assert callable(piper_engine.synthesize)
    assert issubclass(piper_engine.PiperError, Exception)


def test_does_not_import_piper():
    """The GPL boundary depends on never importing piper. No executable line
    may be an `import piper` / `from piper` statement (docstrings/comments
    that mention the rule are fine)."""
    src = (ROOT / "jarvis_sidecar" / "piper_engine.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import piper"), \
            "piper_engine must NOT import piper (GPL boundary)"
        assert not stripped.startswith("from piper"), \
            "piper_engine must NOT import from piper (GPL boundary)"


def test_is_available_false_when_venv_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    assert piper_engine.is_available() is False


def test_is_available_true_when_venv_and_model_present(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    venv_python = tmp_path / "piper-venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    model = tmp_path / "piper-voices" / "en_GB-alan-medium.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"FAKE-ONNX")
    assert piper_engine.is_available() is True


async def test_synthesize_argv_safety(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs.get("cwd")
        f_idx = argv.index("-f")
        pathlib.Path(argv[f_idx + 1]).write_bytes(b"RIFFWAVE-FAKE")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    audio = await piper_engine.synthesize("hello there", voice="en_GB-alan-medium")
    assert audio == b"RIFFWAVE-FAKE"
    argv = captured["argv"]
    assert argv[0].endswith("/piper-venv/bin/python")
    assert argv[1] == "-m" and argv[2] == "piper"
    assert "en_GB-alan-medium" in argv
    assert "-f" in argv
    assert "--" in argv
    assert argv[-1] == "hello there"


async def test_synthesize_rejects_bad_voice_leading_dash(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    with pytest.raises(piper_engine.PiperError, match="voice"):
        await piper_engine.synthesize("hi", voice="--output-raw")


async def test_synthesize_rejects_voice_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    with pytest.raises(piper_engine.PiperError, match="voice"):
        await piper_engine.synthesize("hi", voice="../../etc/passwd")


async def test_synthesize_rejects_oversized_text(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    big = "x" * 2001
    with pytest.raises(piper_engine.PiperError, match="too long"):
        await piper_engine.synthesize(big, voice="en_GB-alan-medium")


async def test_synthesize_rejects_empty_text(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    with pytest.raises(piper_engine.PiperError, match="empty"):
        await piper_engine.synthesize("", voice="en_GB-alan-medium")


async def test_synthesize_nonzero_exit_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))

    async def fake_exec(*argv, **kwargs):
        return _FakeProc(returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(piper_engine.PiperError, match="exit"):
        await piper_engine.synthesize("hi", voice="en_GB-alan-medium")
