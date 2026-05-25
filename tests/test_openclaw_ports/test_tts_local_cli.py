"""
Hermetic tests for openclaw_ports.tts_local_cli.

The port is OS-dependent (macOS `say` binary). Tests mock subprocess
calls so they run on any platform in CI. A live integration test
hitting real `say` lives separately under tests/test_openclaw_ports/
integration/ and is excluded from the default pytest collection.
"""

from __future__ import annotations

import pathlib
import sys

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
