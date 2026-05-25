"""
Hermetic tests for openclaw_ports.tts_local_cli.

The port is OS-dependent (macOS `say` binary). Tests mock subprocess
calls so they run on any platform in CI. A live integration test
hitting real `say` lives separately under tests/test_openclaw_ports/
integration/ and is excluded from the default pytest collection.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from pathlib import Path

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from openclaw_ports import tts_local_cli


def test_module_surface_exists():
    """Public surface per spec §3.2."""
    assert callable(tts_local_cli.is_available)
    assert callable(tts_local_cli.synthesize)
    assert issubclass(tts_local_cli.CLITTSUnavailable, Exception)
    assert issubclass(tts_local_cli.CLITTSError, Exception)


def test_attribution_header_present():
    """Spec §4.2 mandates a per-file attribution preamble."""
    src = (ROOT / "openclaw_ports" / "tts_local_cli.py").read_text(encoding="utf-8")
    assert "Ported from openclaw/extensions/tts-local-cli" in src
    assert "125d82cab2952f87f532106a368d54e526141026" in src
    assert "MIT-licensed" in src
    assert "openclaw_ports/NOTICE.md" in src


def test_is_available_true_on_macos_with_say(monkeypatch):
    monkeypatch.setattr(tts_local_cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        tts_local_cli.shutil,
        "which",
        lambda name: tts_local_cli.SAY_BINARY if name == tts_local_cli.SAY_BINARY else None,
    )
    assert tts_local_cli.is_available() is True


def test_is_available_false_on_linux(monkeypatch):
    monkeypatch.setattr(tts_local_cli.platform, "system", lambda: "Linux")
    # Even if a `say` binary exists on PATH, we refuse non-Darwin.
    monkeypatch.setattr(tts_local_cli.shutil, "which", lambda name: "/usr/bin/say")
    assert tts_local_cli.is_available() is False


def test_is_available_false_if_say_missing(monkeypatch):
    monkeypatch.setattr(tts_local_cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(tts_local_cli.shutil, "which", lambda name: None)
    assert tts_local_cli.is_available() is False


class _FakeProc:
    def __init__(self, returncode: int = 0, audio_bytes: bytes = b"AAC-PAYLOAD"):
        self.returncode = returncode
        self._audio_bytes = audio_bytes

    async def communicate(self):
        return (b"", b"")

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


async def test_synthesize_happy_path(monkeypatch, tmp_path):
    """`synthesize` writes the text via `say`, reads back the M4A bytes."""
    captured_argv: list[list[str]] = []

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured_argv.append(list(argv))
        # Simulate `say` writing the output file.
        # argv: [SAY_BINARY, "-v", voice, "-o", outpath, "--file-format=m4af",
        #        "--data-format=aac", "--", text]
        outpath = Path(argv[4])
        outpath.write_bytes(b"AAC-PAYLOAD")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        tts_local_cli.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(tts_local_cli, "_tempdir", lambda: tmp_path)
    monkeypatch.setattr(tts_local_cli, "is_available", lambda: True)

    audio = await tts_local_cli.synthesize("hello world", voice="Alex", timeout_s=2.0)
    assert audio == b"AAC-PAYLOAD"
    # argv must include the safety-critical separators and flags.
    argv = captured_argv[0]
    assert argv[0] == tts_local_cli.SAY_BINARY
    assert "-v" in argv and "Alex" in argv
    assert "-o" in argv
    assert "--file-format=m4af" in argv and "--data-format=aac" in argv
    # Text is passed via argv after `--` so no shell interpolation.
    assert "--" in argv
    assert argv[-1] == "hello world"


async def test_synthesize_raises_unavailable_off_macos(monkeypatch):
    monkeypatch.setattr(tts_local_cli, "is_available", lambda: False)
    with pytest.raises(tts_local_cli.CLITTSUnavailable):
        await tts_local_cli.synthesize("anything")


async def test_synthesize_raises_on_nonzero_exit(monkeypatch, tmp_path):
    async def fake_proc(*argv, **kwargs):
        return _FakeProc(returncode=1)
    monkeypatch.setattr(tts_local_cli.asyncio, "create_subprocess_exec", fake_proc)
    monkeypatch.setattr(tts_local_cli, "_tempdir", lambda: tmp_path)
    monkeypatch.setattr(tts_local_cli, "is_available", lambda: True)
    with pytest.raises(tts_local_cli.CLITTSError, match="exit 1"):
        await tts_local_cli.synthesize("hello", voice="Alex", timeout_s=2.0)


def test_strip_emojis_removes_pictographic():
    """Ported from OpenClaw's stripEmojis — `say` chokes on raw emoji."""
    assert tts_local_cli._strip_emojis("Hello 🌟 world 🎉") == "Hello world"
    assert tts_local_cli._strip_emojis("👋") == ""
    assert tts_local_cli._strip_emojis("plain ascii") == "plain ascii"


async def test_synthesize_raises_when_only_emojis(monkeypatch, tmp_path):
    """If the input collapses to empty after stripping, raise CLITTSError."""
    monkeypatch.setattr(tts_local_cli, "is_available", lambda: True)
    monkeypatch.setattr(tts_local_cli, "_tempdir", lambda: tmp_path)
    with pytest.raises(tts_local_cli.CLITTSError, match="empty"):
        await tts_local_cli.synthesize("🎉🎉🎉", voice="Alex")


class _SlowFakeProc:
    """Simulates a `say` invocation that never exits."""
    def __init__(self):
        self.returncode = None
        self.killed = False

    async def wait(self):
        # Sleep longer than any reasonable timeout — wait_for will cancel us.
        await asyncio.sleep(60)
        return 0

    def kill(self):
        self.killed = True


async def test_synthesize_enforces_timeout(monkeypatch, tmp_path):
    """If `say` hangs, synthesize() raises CLITTSError and kills the child."""
    slow_proc = _SlowFakeProc()

    async def fake_proc(*argv, **kwargs):
        # Touch the output path so the not-exists branch doesn't mask the timeout.
        outpath = Path(argv[4])
        outpath.write_bytes(b"")
        return slow_proc

    monkeypatch.setattr(tts_local_cli.asyncio, "create_subprocess_exec", fake_proc)
    monkeypatch.setattr(tts_local_cli, "_tempdir", lambda: tmp_path)
    monkeypatch.setattr(tts_local_cli, "is_available", lambda: True)

    with pytest.raises(tts_local_cli.CLITTSError, match="timed out"):
        await tts_local_cli.synthesize("hello", voice="Alex", timeout_s=0.1)
    assert slow_proc.killed is True
