"""
Local CLI text-to-speech via macOS `say`.

Ported from openclaw/extensions/tts-local-cli/speech-provider.ts at
commit 125d82cab2952f87f532106a368d54e526141026.
MIT-licensed; see openclaw_ports/NOTICE.md for full license text.

Resync policy: manual diff against the pinned commit. Bump SHA in
NOTICE.md when forward-porting upstream changes.

The OpenClaw original is a generic CLI-TTS provider with template-
substituted args, ffmpeg conversion, telephony output, and voice-note
opus. JARVIS only needs the macOS `say` path producing browser-
playable audio. We deliberately omit the generic CLI runner, ffmpeg
dependency, and non-mp4 output paths — `say` itself writes AAC in
M4A which Web Audio API decodes natively.
"""

from __future__ import annotations

import asyncio
import platform
import re
import shutil
import tempfile
from pathlib import Path
from typing import Final

SAY_BINARY: Final[str] = "/usr/bin/say"
DEFAULT_VOICE: Final[str] = "Alex"
DEFAULT_TIMEOUT_S: Final[float] = 30.0

# Matches emoji presentation + extended pictographic + variation selectors.
# Mirrors OpenClaw's regex (TypeScript: /[\p{Emoji_Presentation}\p{Extended_Pictographic}]/gu).
_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "]+",
    flags=re.UNICODE,
)


def _strip_emojis(text: str) -> str:
    """Remove emoji/pictographic codepoints and collapse whitespace.

    Ported from OpenClaw's stripEmojis (speech-provider.ts:87-92).
    """
    no_emoji = _EMOJI_RE.sub(" ", text)
    return re.sub(r"\s+", " ", no_emoji).strip()


class CLITTSUnavailable(RuntimeError):
    """Raised when macOS `say` is not present (e.g. inside a Linux container)."""


class CLITTSError(RuntimeError):
    """Raised on synthesis failure, timeout, or empty input after sanitization."""


def is_available() -> bool:
    """Return True iff we're on macOS and the `say` binary is executable.

    Implemented in T3.
    """
    if platform.system() != "Darwin":
        return False
    return shutil.which(SAY_BINARY) is not None


def _tempdir() -> Path:
    """Return the directory to use for temp output files. Overridable in tests."""
    return Path(tempfile.gettempdir())


async def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """Synthesize ``text`` to AAC/M4A audio bytes using macOS `say`.

    Raises:
        CLITTSUnavailable: when `say` is not present.
        CLITTSError: on non-zero exit, missing output file, or empty text.
    """
    if not is_available():
        raise CLITTSUnavailable("macOS `say` binary not found")

    cleaned = _strip_emojis(text)
    if not cleaned:
        raise CLITTSError("text is empty after stripping emojis")

    # Use a unique temp file name in the tempdir so concurrent calls don't collide.
    with tempfile.NamedTemporaryFile(
        prefix="jarvis-tts-", suffix=".m4a", dir=str(_tempdir()), delete=False
    ) as tf:
        outpath = Path(tf.name)

    try:
        # All untrusted values (voice, text) flow through argv AFTER `--`,
        # never through the shell. No string interpolation in argv.
        argv = [
            SAY_BINARY,
            "-v", voice,
            "-o", str(outpath),
            "--file-format=m4af",
            "--data-format=aac",
            "--",
            cleaned,
        ]
        # Discard stdout/stderr — we don't read them, and PIPE without a drain
        # can deadlock if `say` ever fills the OS pipe buffer (~64 KiB on Linux).
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            raise CLITTSError(f"`say` timed out after {timeout_s}s")

        if proc.returncode != 0:
            raise CLITTSError(f"`say` exit {proc.returncode}")

        if not outpath.exists() or outpath.stat().st_size == 0:
            raise CLITTSError("`say` produced no output")

        return outpath.read_bytes()
    finally:
        try:
            outpath.unlink()
        except FileNotFoundError:
            pass
