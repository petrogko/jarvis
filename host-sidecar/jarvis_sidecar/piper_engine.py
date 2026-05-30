"""
Piper neural TTS engine (GPL-3.0) — invoked ONLY as a subprocess.

CRITICAL LICENSE BOUNDARY: this module must NEVER `import piper`. Piper
(OHF-Voice/piper1-gpl) is GPL-3.0; importing it would make JARVIS a
derivative work. We exec the piper venv's python at a process boundary
instead — same arm's-length stance as `say`, `whisper-cli`, `ffmpeg`.
The piper package lives in its OWN venv (config.piper_venv()), separate
from this sidecar's venv, so `import piper` here would fail anyway.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

from . import config

# Required fix #1: voice flows into argv BEFORE the `--` separator, so a
# leading-dash or path value would be parsed by piper as a flag / arbitrary
# model path. Allowlist: must START with alnum/underscore (NOT a dash, else
# piper reads it as a flag), then alnum/hyphen/underscore, total 1-64 chars.
_VOICE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")


class PiperError(RuntimeError):
    """Piper synthesis failure or invalid input."""


def _venv_python() -> Path:
    return config.piper_venv() / "bin" / "python"


def is_available() -> bool:
    """True iff the piper venv python and the default voice .onnx both exist."""
    model = config.piper_data_dir() / f"{config.DEFAULT_PIPER_VOICE}.onnx"
    return _venv_python().exists() and model.exists()


async def synthesize(text: str, voice: str) -> bytes:
    """Synthesize `text` to WAV bytes via the piper CLI (subprocess-only).

    Raises PiperError on: empty text, text over the cap, invalid voice name,
    subprocess failure, timeout, or missing output.
    """
    if not text or not text.strip():
        raise PiperError("text is empty")
    if len(text) > config.PIPER_MAX_TEXT_CHARS:
        raise PiperError(f"text too long (>{config.PIPER_MAX_TEXT_CHARS} chars)")
    if not _VOICE_RE.match(voice):
        raise PiperError(f"invalid voice name: {voice!r}")

    with tempfile.NamedTemporaryFile(
        prefix="jarvis-sidecar-piper-", suffix=".wav", delete=False
    ) as tf:
        outpath = Path(tf.name)

    try:
        argv = [
            str(_venv_python()),
            "-m", "piper",
            "-m", voice,
            "-f", str(outpath),
            "--",
            text,
        ]
        # cwd = data dir so piper resolves the voice model by name.
        # DEVNULL on both streams — no synthesis fragments in any log.
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(config.piper_data_dir()),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=config.PIPER_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            raise PiperError(f"piper timed out after {config.PIPER_TIMEOUT_S}s")

        if proc.returncode != 0:
            raise PiperError(f"piper exited with code {proc.returncode}")

        if not outpath.exists() or outpath.stat().st_size == 0:
            raise PiperError("piper produced no output")

        return outpath.read_bytes()
    finally:
        try:
            outpath.unlink()
        except FileNotFoundError:
            pass
