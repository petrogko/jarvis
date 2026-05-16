"""
Local-token auth for JARVIS.

Defense-in-depth on top of the default loopback bind. Requests from
127.0.0.1 / ::1 bypass auth (single-user local case). Requests from
any other source must present X-JARVIS-Token (REST) or ?token=
(WebSocket handshake).

The token is generated once on first start, persisted under data/ with
mode 0600, and printed to stdout at startup so a user who knowingly
opens --host can copy it to a remote client.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("jarvis.auth")

_TOKEN_PATH = Path(__file__).parent / "data" / ".local_token"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Paths that never require auth. Health is for liveness probes; the
# static frontend assets must load before the client can present a token.
_PUBLIC_PATHS: tuple[str, ...] = ("/api/health",)
_PUBLIC_PREFIXES: tuple[str, ...] = ("/assets/",)
_PUBLIC_EXACT: frozenset[str] = frozenset({"/", "/favicon.ico"})


def load_or_create_token() -> str:
    """Read the persisted token, or generate and persist one (0600)."""
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _TOKEN_PATH.exists():
        try:
            existing = _TOKEN_PATH.read_text().strip()
            if existing:
                return existing
        except OSError as e:
            log.warning("could not read local token (%s); regenerating", e)
    token = secrets.token_urlsafe(32)
    _TOKEN_PATH.write_text(token)
    try:
        os.chmod(_TOKEN_PATH, 0o600)
    except OSError:
        pass
    return token


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    # Strip IPv6 zone identifiers and any bracket notation.
    cleaned = host.strip().strip("[").strip("]").split("%", 1)[0]
    return cleaned in _LOOPBACK_HOSTS


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT or path in _PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def check_token(presented: str | None, expected: str) -> bool:
    if not presented:
        return False
    return constant_time_equal(presented, expected)


class LocalTokenAuthMiddleware(BaseHTTPMiddleware):
    """
    Enforce X-JARVIS-Token on non-loopback HTTP requests.

    - OPTIONS (CORS preflight) is allowed through unconditionally; the
      response will still be filtered by the CORS middleware.
    - Loopback-sourced requests bypass the check.
    - Public paths (health, static assets, index) are always allowed.
    """

    def __init__(self, app, *, token: str, trust_loopback: bool = True):
        super().__init__(app)
        self._token = token
        self._trust_loopback = trust_loopback

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        if _is_public(request.url.path):
            return await call_next(request)
        client_host = request.client.host if request.client else None
        if self._trust_loopback and is_loopback(client_host):
            return await call_next(request)
        presented = request.headers.get("x-jarvis-token") or request.query_params.get("token")
        if not check_token(presented, self._token):
            log.warning(
                "auth: rejected %s %s from %s",
                request.method, request.url.path, client_host,
            )
            return JSONResponse(
                {"error": "unauthorized", "detail": "missing or invalid X-JARVIS-Token"},
                status_code=401,
            )
        return await call_next(request)


def websocket_authorized(ws, expected_token: str, *, trust_loopback: bool = True) -> bool:
    """
    Decide whether a WebSocket upgrade request is allowed. Call BEFORE
    `await ws.accept()`. Returns False if the caller should close with
    a 4401 policy-violation code.
    """
    client_host = ws.client.host if ws.client else None
    if trust_loopback and is_loopback(client_host):
        return True
    presented = ws.headers.get("x-jarvis-token") or ws.query_params.get("token")
    return check_token(presented, expected_token)
