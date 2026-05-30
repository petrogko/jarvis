"""
JARVIS → host-sidecar client.

The sidecar runs on the macOS host (NOT in the JARVIS container) at
http://host.docker.internal:9999. Auth is a shared-secret token mounted
RO into the container at /host-sidecar-config/token. See
docs/superpowers/specs/2026-05-26-jarvis-sidecar-design.md.

Per security-advisor required fix #5: `stt_via_sidecar`'s return value
flows back through the SAME voice-handler pipeline as Web Speech
transcripts (server.py /ws/voice transcript handler). No bypass path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

import httpx

log = logging.getLogger("jarvis.sidecar")

_TOKEN_PATH: Path = Path("/host-sidecar-config/token")
_HEADER_NAME: Final[str] = "X-SIDECAR-Token"


def _read_token() -> str:
    try:
        return _TOKEN_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _base_url() -> str:
    # Read from vault settings (server.py supplies _vault_get). Lazy import to
    # avoid circular deps at module load.
    try:
        from server import _vault_get
        return _vault_get("SIDECAR_URL", "http://host.docker.internal:9999")
    except Exception:
        return "http://host.docker.internal:9999"


async def tts_via_sidecar(text: str, voice: str = "Alex") -> bytes | None:
    """POST /tts. Returns audio bytes on 200; None on any failure."""
    token = _read_token()
    if not token:
        log.warning("sidecar token not available; skipping sidecar TTS")
        return None
    url = f"{_base_url()}/tts"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            r = await http.post(
                url,
                headers={_HEADER_NAME: token},
                json={"text": text, "voice": voice},
            )
        if r.status_code != 200:
            log.warning("sidecar /tts returned %s", r.status_code)
            return None
        return r.content
    except httpx.HTTPError as e:
        log.info("sidecar /tts unreachable: %s", e)
        return None


async def stt_via_sidecar(audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
    """POST /stt. Returns transcript on 200; empty string on any failure."""
    token = _read_token()
    if not token:
        log.warning("sidecar token not available; skipping sidecar STT")
        return ""
    url = f"{_base_url()}/stt"
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            files = {"audio": ("upload.bin", audio_bytes, mime_type)}
            r = await http.post(url, headers={_HEADER_NAME: token}, files=files)
        if r.status_code != 200:
            log.warning("sidecar /stt returned %s", r.status_code)
            return ""
        return r.json().get("text", "")
    except httpx.HTTPError as e:
        log.info("sidecar /stt unreachable: %s", e)
        return ""


async def sidecar_health() -> dict | None:
    """GET /health. Returns the dict on 200; None on any failure."""
    token = _read_token()
    if not token:
        return None
    url = f"{_base_url()}/health"
    try:
        async with httpx.AsyncClient(timeout=3.0) as http:
            r = await http.get(url, headers={_HEADER_NAME: token})
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


# ---------------------------------------------------------------------------
# /spawn — claude -p on the host for JARVIS-in-Docker (see spec 2026-05-29).
# ---------------------------------------------------------------------------

async def spawn_via_sidecar(
    prompt: str,
    workdir: str,
    *,
    timeout_s: float = 300.0,
    agent: str = "claude",
) -> dict | None:
    """POST /spawn. Returns {session_id, status, started_at} on 200; None on
    any failure (auth, network, validation 4xx, admission 429, server 5xx)."""
    token = _read_token()
    if not token:
        log.warning("sidecar token not available; skipping sidecar /spawn")
        return None
    url = f"{_base_url()}/spawn"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            r = await http.post(
                url,
                headers={_HEADER_NAME: token},
                json={"prompt": prompt, "workdir": workdir,
                      "agent": agent, "timeout_s": timeout_s},
            )
        if r.status_code != 200:
            log.warning("sidecar /spawn returned %s: %s", r.status_code, r.text[:200])
            return None
        return r.json()
    except httpx.HTTPError as e:
        log.info("sidecar /spawn unreachable: %s", e)
        return None


async def spawn_status(session_id: str) -> dict | None:
    """GET /spawn/{session_id}. Returns the session dict on 200; None on
    404/network failure. Caller polls this until status != 'running'."""
    token = _read_token()
    if not token:
        return None
    url = f"{_base_url()}/spawn/{session_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.get(url, headers={_HEADER_NAME: token})
        if r.status_code != 200:
            return None
        return r.json()
    except httpx.HTTPError:
        return None


async def spawn_kill(session_id: str) -> dict | None:
    """DELETE /spawn/{session_id}. Returns the final session dict on 200;
    None on 404/network failure."""
    token = _read_token()
    if not token:
        return None
    url = f"{_base_url()}/spawn/{session_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.delete(url, headers={_HEADER_NAME: token})
        if r.status_code != 200:
            return None
        return r.json()
    except httpx.HTTPError:
        return None
