"""
FastAPI app factory. Wires endpoints, the token auth dependency, and reads
the token from disk at startup.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel

from . import config
from .auth import HEADER_NAME, header_matches
from .tts import synthesize, TTSError
from .stt import transcribe, STTError
from .cwd_allowlist import check_workdir
from .spawn import (
    SpawnManager,
    SpawnError,
    caller_fingerprint,
    claude_available,
    _audit_write,
    _iso,
)
from .piper_engine import (
    synthesize as piper_synthesize,
    is_available as piper_is_available,
    PiperError,
)


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


class _TTSBody(BaseModel):
    text: str
    voice: str = "Alex"
    engine: str = "say"


class _SpawnBody(BaseModel):
    prompt: str
    workdir: str
    agent: str = "claude"
    timeout_s: float = config.SPAWN_DEFAULT_TIMEOUT_S


def create_app() -> FastAPI:
    app = FastAPI(title="jarvis-sidecar", version="0.1.0")
    token = _load_token()
    spawn_manager = SpawnManager()

    def require_token(x_sidecar_token: str | None = Header(None, alias=HEADER_NAME)) -> str:
        """Validate the X-SIDECAR-Token. Returns the matched token so handlers
        can compute the caller fingerprint for audit logs."""
        if not header_matches(token, x_sidecar_token):
            raise HTTPException(status_code=401, detail="invalid or missing X-SIDECAR-Token")
        return x_sidecar_token or ""

    @app.get("/health")
    def health(_=Depends(require_token)) -> dict:
        return {
            "status": "ok",
            "whisper_model": _whisper_model_name(),
            "say_available": _say_available(),
            "spawn_ready": claude_available(),
            "piper_available": piper_is_available(),
        }

    @app.post("/tts")
    async def tts(body: _TTSBody, _auth: None = Depends(require_token)) -> Response:
        # Piper path (GPL subprocess). Falls back to say if unavailable.
        if body.engine == "piper" and piper_is_available():
            try:
                audio = await piper_synthesize(body.text, body.voice)
            except PiperError as e:
                msg = str(e)
                if any(k in msg.lower() for k in ("empty", "too long", "invalid voice")):
                    raise HTTPException(status_code=400, detail=msg)
                raise HTTPException(status_code=500, detail=msg)
            return Response(
                content=audio, media_type="audio/wav",
                headers={"X-TTS-Engine-Used": "piper"},
            )

        # say path (default + fallback).
        try:
            audio = await synthesize(body.text, body.voice)
        except TTSError as e:
            msg = str(e)
            if "empty" in msg.lower():
                raise HTTPException(status_code=400, detail=msg)
            raise HTTPException(status_code=500, detail=msg)
        return Response(
            content=audio, media_type="audio/m4a",
            headers={"X-TTS-Engine-Used": "say"},
        )

    @app.post("/stt")
    async def stt(
        audio: UploadFile = File(...),
        _=Depends(require_token),
    ) -> dict:
        # Cap at config.STT_MAX_BYTES (defense against resource exhaustion).
        contents = await audio.read(config.STT_MAX_BYTES + 1)
        if len(contents) > config.STT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="audio upload too large")
        try:
            text, duration_ms = await transcribe(contents, audio.content_type or "audio/webm")
        except STTError as e:
            raise HTTPException(status_code=500, detail=str(e))
        return {"text": text, "duration_ms": duration_ms}

    # ------------------------------------------------------------------
    # /spawn — claude -p on the host (see spec 2026-05-29).
    # ------------------------------------------------------------------

    @app.post("/spawn")
    async def spawn(body: _SpawnBody, token_in: str = Depends(require_token)) -> dict:
        # 1. Agent allowlist.
        if body.agent != "claude":
            raise HTTPException(status_code=400, detail=f"unknown agent: {body.agent!r}")

        # 2. Prompt cap.
        prompt_bytes = body.prompt.encode("utf-8")
        if not body.prompt or not body.prompt.strip():
            raise HTTPException(status_code=400, detail="prompt is empty")
        if len(prompt_bytes) > config.PROMPT_MAX_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"prompt too large ({len(prompt_bytes)} > {config.PROMPT_MAX_BYTES})",
            )

        # 3. Timeout range.
        if (body.timeout_s < config.SPAWN_MIN_TIMEOUT_S
                or body.timeout_s > config.SPAWN_MAX_TIMEOUT_S):
            raise HTTPException(
                status_code=400,
                detail=f"timeout_s must be in [{config.SPAWN_MIN_TIMEOUT_S}, "
                       f"{config.SPAWN_MAX_TIMEOUT_S}]",
            )

        caller_fp = caller_fingerprint(token_in)

        # 4. Workdir allowlist + symlink + denied components.
        ok, reason = check_workdir(body.workdir)
        if not ok:
            _audit_write({
                "ts": _iso(__import__("time").time()),
                "verb": "reject",
                "caller_fingerprint": caller_fp,
                "workdir": body.workdir,
                "prompt_bytes": len(prompt_bytes),
                "reason": reason,
            })
            raise HTTPException(status_code=400, detail=reason)

        # 5. Admit + spawn.
        try:
            session = await spawn_manager.spawn(
                body.prompt, body.workdir, body.timeout_s, caller_fp,
            )
        except SpawnError as e:
            msg = str(e)
            if "concurrent" in msg or "rate cap" in msg:
                raise HTTPException(status_code=429, detail=msg)
            raise HTTPException(status_code=500, detail=msg)

        return {
            "session_id": session.session_id,
            "status": session.status,
            "started_at": session.started_at,
        }

    @app.get("/spawn/{session_id}")
    async def spawn_get(session_id: str, _=Depends(require_token)) -> dict:
        session = spawn_manager.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown or expired session")
        return session.to_response()

    @app.delete("/spawn/{session_id}")
    async def spawn_delete(session_id: str, token_in: str = Depends(require_token)) -> dict:
        caller_fp = caller_fingerprint(token_in)
        session = await spawn_manager.kill(session_id, caller_fp)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown or expired session")
        return session.to_response()

    return app


# Module-level app for `uvicorn jarvis_sidecar.app:app` invocations.
# Loaded lazily; create_app() raises if the token is missing.
try:
    app = create_app()
except RuntimeError:
    # Allow `python -m jarvis_sidecar` to import without exploding when the
    # token doesn't exist yet (setup.sh path).
    app = None
