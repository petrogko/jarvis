"""
Local STT via whisper.cpp's `whisper-cli` binary.

Flow: incoming audio blob → ffmpeg → 16kHz mono WAV → whisper-cli → text.
All three temp files cleaned in `finally`. Both subprocesses run with
DEVNULL stdout (per security-advisor required fix #3) so transcript
fragments never appear in the rotating log.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from . import config


class STTError(RuntimeError):
    """STT failure (subprocess error, timeout, missing output)."""


def _ffmpeg_path() -> str:
    p = shutil.which("ffmpeg")
    return p or "ffmpeg"


def _whisper_path() -> str:
    p = shutil.which("whisper-cli")
    return p or "whisper-cli"


async def transcribe(audio_bytes: bytes, mime_hint: str = "audio/webm") -> tuple[str, int]:
    """Transcribe `audio_bytes`. Returns (text, duration_ms).

    Raises STTError on any subprocess failure or timeout.
    """
    started = time.monotonic()
    workdir = Path(tempfile.mkdtemp(prefix="jarvis-sidecar-stt-"))
    upload = workdir / "upload.bin"
    wav = workdir / "pcm.wav"
    out_prefix = workdir / "out"

    try:
        upload.write_bytes(audio_bytes)

        # ffmpeg: re-encode to 16kHz mono WAV (what whisper.cpp wants).
        ff_argv = [
            _ffmpeg_path(),
            "-y",
            "-i", str(upload),
            "-ar", "16000",
            "-ac", "1",
            "-f", "wav",
            str(wav),
        ]
        ff = await asyncio.create_subprocess_exec(
            *ff_argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(ff.wait(), timeout=config.WHISPER_TIMEOUT_S)
        except asyncio.TimeoutError:
            ff.kill()
            raise STTError("ffmpeg timed out")
        if ff.returncode != 0:
            raise STTError(f"ffmpeg exited {ff.returncode}")

        # whisper-cli: -nt no timestamps, -np no progress, -otxt write text to
        # <prefix>.txt. stdout is DEVNULL so no transcript text reaches the
        # rotating log (security-advisor required fix #3).
        model = config.model_dir() / config.DEFAULT_WHISPER_MODEL
        wh_argv = [
            _whisper_path(),
            "-m", str(model),
            "-f", str(wav),
            "-nt",
            "-np",
            "-otxt",
            "-of", str(out_prefix),
        ]
        wh = await asyncio.create_subprocess_exec(
            *wh_argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(wh.wait(), timeout=config.WHISPER_TIMEOUT_S)
        except asyncio.TimeoutError:
            wh.kill()
            raise STTError("whisper-cli timed out")
        if wh.returncode != 0:
            raise STTError(f"whisper-cli exited {wh.returncode}")

        text_path = out_prefix.with_suffix(".txt")
        if not text_path.exists():
            raise STTError("whisper-cli produced no output file")
        text = text_path.read_text(encoding="utf-8").strip()

        duration_ms = int((time.monotonic() - started) * 1000)
        return text, duration_ms
    finally:
        # Clean ALL temp files; swallow FileNotFoundError per security-advisor
        # required fix #4.
        for p in (upload, wav, out_prefix.with_suffix(".txt")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass
