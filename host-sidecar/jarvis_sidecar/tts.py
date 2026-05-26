"""
Local TTS via macOS `say`. Mirrors `openclaw_ports/tts_local_cli.py` from
the main JARVIS repo — argv-only invocation, DEVNULL on both stdout and
stderr, temp file cleanup in finally.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from . import config

SAY_BINARY = "/usr/bin/say"


class TTSError(RuntimeError):
    """Synthesis failure (non-zero exit, missing output, timeout)."""


async def synthesize(text: str, voice: str) -> bytes:
    """Produce AAC/M4A audio bytes for `text` using `say -v <voice>`.

    Argv-only — text and voice flow as argv items after `--`. No shell, no
    f-string interpolation.

    Raises TTSError on subprocess failure or empty input.
    """
    if not text or not text.strip():
        raise TTSError("text is empty")

    with tempfile.NamedTemporaryFile(
        prefix="jarvis-sidecar-tts-", suffix=".m4a", delete=False
    ) as tf:
        outpath = Path(tf.name)

    try:
        argv = [
            SAY_BINARY,
            "-v", voice,
            "-o", str(outpath),
            "--file-format=m4af",
            "--data-format=aac",
            "--",
            text,
        ]
        # Discard stdout/stderr — DEVNULL prevents pipe deadlocks and matches
        # the openclaw_ports/tts_local_cli pattern.
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=config.SAY_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            raise TTSError(f"`say` timed out after {config.SAY_TIMEOUT_S}s")

        if proc.returncode != 0:
            raise TTSError(f"`say` exited with code {proc.returncode}")

        if not outpath.exists() or outpath.stat().st_size == 0:
            raise TTSError("`say` produced no output")

        return outpath.read_bytes()
    finally:
        try:
            outpath.unlink()
        except FileNotFoundError:
            pass
