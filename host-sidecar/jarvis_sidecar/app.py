"""
FastAPI app factory. Wires endpoints, the token auth dependency, and reads
the token from disk at startup.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException

from . import config
from .auth import HEADER_NAME, header_matches


def _load_token() -> str:
    path = config.token_path()
    if not path.exists():
        # In production, setup.sh creates this; in tests, the fixture writes it.
        raise RuntimeError(f"sidecar token not found at {path}")
    return path.read_text(encoding="utf-8").strip()


def _say_available() -> bool:
    return shutil.which("/usr/bin/say") is not None


def _whisper_model_name() -> str:
    """Returns the configured Whisper model filename (may not exist on disk yet)."""
    return config.DEFAULT_WHISPER_MODEL


def create_app() -> FastAPI:
    app = FastAPI(title="jarvis-sidecar", version="0.1.0")
    token = _load_token()

    def require_token(x_sidecar_token: str | None = Header(None, alias=HEADER_NAME)) -> None:
        if not header_matches(token, x_sidecar_token):
            raise HTTPException(status_code=401, detail="invalid or missing X-SIDECAR-Token")

    @app.get("/health")
    def health(_=Depends(require_token)) -> dict:
        return {
            "status": "ok",
            "whisper_model": _whisper_model_name(),
            "say_available": _say_available(),
        }

    return app


# Module-level app for `uvicorn jarvis_sidecar.app:app` invocations.
# Loaded lazily; create_app() raises if the token is missing.
try:
    app = create_app()
except RuntimeError:
    # Allow `python -m jarvis_sidecar` to import without exploding when the
    # token doesn't exist yet (setup.sh path).
    app = None
