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


async def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """Synthesize ``text`` to AAC/M4A audio bytes using macOS `say`.

    Implemented in T4–T6.
    """
    raise NotImplementedError
