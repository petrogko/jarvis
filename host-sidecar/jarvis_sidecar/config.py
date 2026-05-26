"""Sidecar configuration constants and on-disk paths.

The token is the JARVIS↔sidecar shared secret. Lives at a fixed XDG-style
path on the macOS host. JARVIS reads the SAME file via a Docker bind-mount
(see docs/superpowers/specs/2026-05-26-jarvis-sidecar-design.md §6).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

# Loopback bind — hardcoded, never read from env. Sidecar must NEVER listen
# on 0.0.0.0 or any non-127.0.0.1 address.
BIND_HOST: Final[str] = "127.0.0.1"
BIND_PORT: Final[int] = 9999

# Per-user state directory on macOS. Overridable for tests via env.
def state_dir() -> Path:
    override = os.environ.get("JARVIS_SIDECAR_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "jarvis-sidecar"


def token_path() -> Path:
    return state_dir() / "token"


def model_dir() -> Path:
    return state_dir() / "models"


# Default Whisper model file under model_dir().
DEFAULT_WHISPER_MODEL: Final[str] = "ggml-base.en.bin"

# Hard timeouts (seconds) for the wrapped binaries.
SAY_TIMEOUT_S: Final[float] = 30.0
WHISPER_TIMEOUT_S: Final[float] = 60.0

# Multipart upload cap for /stt — defends against resource exhaustion.
STT_MAX_BYTES: Final[int] = 5 * 1024 * 1024  # 5 MiB
