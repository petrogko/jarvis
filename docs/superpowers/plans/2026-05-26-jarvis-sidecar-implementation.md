# jarvis-sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the combined macOS host-sidecar (`jarvis-sidecar`) that exposes `/tts` (wraps `say`) and `/stt` (wraps `whisper-cli`) on `127.0.0.1:9999`, plus the JARVIS backend client + frontend STT path. Per `docs/superpowers/specs/2026-05-26-jarvis-sidecar-design.md`.

**Architecture:** Standalone Python/FastAPI daemon under `host-sidecar/` at repo root, launchctl-managed. JARVIS Docker calls it via `host.docker.internal` with a bind-mounted shared-secret (`X-SIDECAR-Token`). Frontend records audio with MediaRecorder, POSTs blob to JARVIS `/api/stt`, which proxies to the sidecar.

**Tech Stack:** Python 3.11, FastAPI, uvicorn, `httpx` (existing dep). Sidecar binaries: `whisper-cpp`, `ffmpeg` (brew). macOS `say` (built-in). No new Python deps in JARVIS.

**Branch:** `feat/sidecar-implementation` (current).

**Persona gates** (per CLAUDE.md routing + saved feedback):
- `security-advisor` — already reviewed the spec; per-task re-review fires automatically only on edits to `SECURITY.md` / `ARCHITECTURE.md` (T11). Sidecar code is NEW surface; reviewer pass at T9 + final-branch review at T12.
- `code-reviewer` — invoked on EVERY task that touches ≥30 LOC or any auth path. Per the saved feedback this is non-negotiable.
- `test-runner` — at T12 before any "ready" claim.

---

## Task 1: Scaffold `host-sidecar/` package

**Files:**
- Create: `host-sidecar/pyproject.toml`
- Create: `host-sidecar/jarvis_sidecar/__init__.py`
- Create: `host-sidecar/jarvis_sidecar/config.py`
- Create: `host-sidecar/jarvis_sidecar/__main__.py`
- Create: `host-sidecar/README.md`
- Create: `host-sidecar/tests/__init__.py`

**Parallelism:** Blocks T2–T5. `[SEQUENTIAL]`

- [ ] **Step 1: Create `host-sidecar/pyproject.toml`**

```toml
[project]
name = "jarvis-sidecar"
version = "0.1.0"
description = "macOS host sidecar for JARVIS: local TTS via `say` and STT via whisper.cpp."
requires-python = ">=3.11"
dependencies = [
    "fastapi==0.115.5",
    "uvicorn[standard]==0.32.1",
    "python-multipart==0.0.20",
]

[project.optional-dependencies]
dev = [
    "pytest==9.0.3",
    "httpx==0.27.2",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["jarvis_sidecar*"]
```

- [ ] **Step 2: Create `host-sidecar/jarvis_sidecar/__init__.py`**

```python
"""jarvis-sidecar — macOS host TTS/STT daemon for JARVIS."""
```

- [ ] **Step 3: Create `host-sidecar/jarvis_sidecar/config.py`**

```python
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
```

- [ ] **Step 4: Create `host-sidecar/jarvis_sidecar/__main__.py`** (entry point — minimal stub for now; the FastAPI app comes in T2)

```python
"""Entry point: `python -m jarvis_sidecar`."""

from __future__ import annotations

import sys


def main() -> int:
    # Wired in T2 once the FastAPI app exists.
    print("jarvis-sidecar: entry-point stub. T2 wires the FastAPI app.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Create `host-sidecar/tests/__init__.py`** (empty)

```python
```

- [ ] **Step 6: Create `host-sidecar/README.md`**

```markdown
# jarvis-sidecar

macOS host daemon for JARVIS. Exposes local TTS (`say`) and STT (`whisper-cli`)
to the JARVIS Docker container over loopback HTTP.

See `docs/superpowers/specs/2026-05-26-jarvis-sidecar-design.md` for the design
and trust model.

## Quick start

```bash
./setup.sh    # T9 — installs brew deps + downloads model + writes token + loads launchctl plist
```

## Uninstall

```bash
./teardown.sh # T9 — unloads launchctl plist + removes token + state dir
```

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | /health | X-SIDECAR-Token | service status |
| POST | /tts | X-SIDECAR-Token | text → AAC/M4A audio bytes |
| POST | /stt | X-SIDECAR-Token | audio multipart → transcript text |

Default bind: `127.0.0.1:9999`. Never exposed to LAN.
```

- [ ] **Step 7: Commit**

```bash
git add host-sidecar/
git commit -m "feat(sidecar): scaffold host-sidecar/ package + config constants

Empty FastAPI package + on-disk path constants + multipart upload cap
per docs/superpowers/specs/2026-05-26-jarvis-sidecar-design.md §3.1.
Entry point is a stub; T2 wires the actual app."
```

---

## Task 2: `/health` endpoint + token auth middleware

**Files:**
- Create: `host-sidecar/jarvis_sidecar/auth.py`
- Create: `host-sidecar/jarvis_sidecar/app.py`
- Create: `host-sidecar/tests/test_auth.py`
- Create: `host-sidecar/tests/test_health.py`
- Modify: `host-sidecar/jarvis_sidecar/__main__.py`

**Parallelism:** `[SEQUENTIAL after T1]`. Blocks T3–T5.

- [ ] **Step 1: Write failing test for auth dependency**

Create `host-sidecar/tests/test_auth.py`:

```python
"""
Hermetic tests for jarvis_sidecar.auth.

The auth model is a single shared-secret token compared with constant-time
equality. Missing or wrong header → 401.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis_sidecar import auth


def test_constant_time_equal_matches_identical():
    assert auth._constant_time_equal("abc", "abc") is True


def test_constant_time_equal_rejects_different():
    assert auth._constant_time_equal("abc", "abd") is False


def test_constant_time_equal_rejects_different_lengths():
    assert auth._constant_time_equal("abc", "abcd") is False


def test_constant_time_equal_rejects_empty_inputs():
    assert auth._constant_time_equal("", "abc") is False
    assert auth._constant_time_equal("abc", "") is False
    assert auth._constant_time_equal("", "") is False
```

- [ ] **Step 2: Run to verify failure**

```bash
cd host-sidecar && python -m pytest tests/test_auth.py -v 2>&1 | /usr/bin/tail -n 10
```
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvis_sidecar.auth'`.

- [ ] **Step 3: Implement `host-sidecar/jarvis_sidecar/auth.py`**

```python
"""
Shared-secret token auth for the sidecar.

The token is read once at app startup. Subsequent comparisons use
hmac.compare_digest for constant-time equality (defense against timing
oracles).
"""

from __future__ import annotations

import hmac
from typing import Final

# Header name — distinct from the browser↔JARVIS X-JARVIS-Token, per
# security-advisor non-blocking recommendation #3 (disambiguation).
HEADER_NAME: Final[str] = "X-SIDECAR-Token"


def _constant_time_equal(a: str, b: str) -> bool:
    """Constant-time string comparison. Returns False on length mismatch."""
    if not a or not b or len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


def header_matches(expected: str, presented: str | None) -> bool:
    """True iff `presented` equals `expected` in constant time."""
    if presented is None:
        return False
    return _constant_time_equal(expected, presented)
```

- [ ] **Step 4: Run auth tests — expect 4 PASSED**

```bash
cd host-sidecar && python -m pytest tests/test_auth.py -v 2>&1 | /usr/bin/tail -n 10
```

- [ ] **Step 5: Write failing test for /health endpoint**

Create `host-sidecar/tests/test_health.py`:

```python
"""
Hermetic tests for the /health endpoint via FastAPI TestClient.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Isolated state dir + a fixed test token."""
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    (tmp_path / "token").write_text("test-token-12345", encoding="utf-8")
    # Re-import the app after env is set so it reads the fresh token.
    if "jarvis_sidecar.app" in sys.modules:
        del sys.modules["jarvis_sidecar.app"]
    from jarvis_sidecar.app import create_app
    return TestClient(create_app())


def test_health_requires_token(client):
    r = client.get("/health")
    assert r.status_code == 401


def test_health_rejects_wrong_token(client):
    r = client.get("/health", headers={"X-SIDECAR-Token": "wrong"})
    assert r.status_code == 401


def test_health_ok_with_correct_token(client):
    r = client.get("/health", headers={"X-SIDECAR-Token": "test-token-12345"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "whisper_model" in body
    assert "say_available" in body
```

- [ ] **Step 6: Run to verify failure**

```bash
cd host-sidecar && python -m pytest tests/test_health.py -v 2>&1 | /usr/bin/tail -n 10
```
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvis_sidecar.app'`.

- [ ] **Step 7: Implement `host-sidecar/jarvis_sidecar/app.py`**

```python
"""
FastAPI app factory. Wires endpoints, the token auth dependency, and reads
the token from disk at startup.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

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
    def health(_=None) -> dict:
        # We want auth on /health (security-advisor recommendation #1: defense-
        # in-depth on top of loopback bind). Use a fresh dep call to keep the
        # decorator readable.
        return {
            "status": "ok",
            "whisper_model": _whisper_model_name(),
            "say_available": _say_available(),
        }

    # Apply the dep to /health by re-declaring with Depends. We do it via a
    # FastAPI dependency override on the route directly:
    from fastapi import Depends

    @app.get("/health", include_in_schema=False)
    def health_authed(_=Depends(require_token)) -> dict:
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
```

WAIT — there are TWO `/health` handlers declared. FastAPI takes the last one. Fix this: use ONLY the authed handler. The plan code above is wrong. Delete the first `health` function and keep only `health_authed`:

```python
def create_app() -> FastAPI:
    app = FastAPI(title="jarvis-sidecar", version="0.1.0")
    token = _load_token()

    from fastapi import Depends

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


try:
    app = create_app()
except RuntimeError:
    app = None
```

This is the production version. The implementer MUST use this single-handler version; the earlier two-handler block was a draft error.

- [ ] **Step 8: Run health tests — expect 3 PASSED**

```bash
cd host-sidecar && python -m pytest tests/test_health.py -v 2>&1 | /usr/bin/tail -n 10
```

- [ ] **Step 9: Wire __main__.py to uvicorn**

Replace `host-sidecar/jarvis_sidecar/__main__.py`:

```python
"""Entry point: `python -m jarvis_sidecar`."""

from __future__ import annotations

import sys

import uvicorn

from . import config


def main() -> int:
    uvicorn.run(
        "jarvis_sidecar.app:app",
        host=config.BIND_HOST,
        port=config.BIND_PORT,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 10: Commit**

```bash
git add host-sidecar/
git commit -m "feat(sidecar): /health endpoint + X-SIDECAR-Token auth dependency

Per spec §3.2 + security-advisor required fix #5 + non-blocking rec #1
(auth on /health). Constant-time token compare. App factory wires the
token once at startup. Hermetic tests for both the auth helper and the
endpoint via TestClient."
```

---

## Task 3: `/tts` endpoint (wraps `say`)

**Files:**
- Create: `host-sidecar/jarvis_sidecar/tts.py`
- Modify: `host-sidecar/jarvis_sidecar/app.py`
- Create: `host-sidecar/tests/test_tts.py`

**Parallelism:** `[SEQUENTIAL after T2]`.

- [ ] **Step 1: Write failing test**

Create `host-sidecar/tests/test_tts.py`:

```python
"""
Hermetic /tts tests — mocks asyncio.create_subprocess_exec so we don't
need a real `say` binary on CI Linux.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = "test-token-12345"
HEADERS = {"X-SIDECAR-Token": TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    (tmp_path / "token").write_text(TOKEN, encoding="utf-8")
    for mod in list(sys.modules):
        if mod.startswith("jarvis_sidecar"):
            del sys.modules[mod]
    from jarvis_sidecar.app import create_app
    return TestClient(create_app()), tmp_path


class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


async def test_tts_argv_safety(client, monkeypatch):
    """Voice + text pass through argv after `--`. No shell."""
    c, tmp = client
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        # Simulate `say` writing the m4a file. argv index 4 is the output path.
        outpath = pathlib.Path(argv[4])
        outpath.write_bytes(b"AAC-PAYLOAD-FAKE")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    r = c.post("/tts", json={"text": "hello world", "voice": "Alex"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.content == b"AAC-PAYLOAD-FAKE"
    assert r.headers["content-type"] == "audio/m4a"
    argv = captured["argv"]
    assert argv[0] == "/usr/bin/say"
    assert "-v" in argv and "Alex" in argv
    assert "-o" in argv
    assert "--file-format=m4af" in argv and "--data-format=aac" in argv
    assert "--" in argv
    assert argv[-1] == "hello world"  # text passes via argv, not shell


def test_tts_requires_token(client):
    c, _ = client
    r = c.post("/tts", json={"text": "x", "voice": "Alex"})
    assert r.status_code == 401


async def test_tts_say_exit_nonzero_returns_500(client, monkeypatch):
    c, _ = client

    async def fake_exec(*argv, **kwargs):
        return _FakeProc(returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    r = c.post("/tts", json={"text": "hello", "voice": "Alex"}, headers=HEADERS)
    assert r.status_code == 500


def test_tts_rejects_empty_text(client):
    c, _ = client
    r = c.post("/tts", json={"text": "", "voice": "Alex"}, headers=HEADERS)
    assert r.status_code == 400
```

These tests use FastAPI TestClient (which is sync) + async monkeypatching. The implementer needs `pytest-asyncio` in dev requirements (already in JARVIS's `requirements-dev.txt`; add `pytest-asyncio==0.24.0` to `host-sidecar/pyproject.toml [project.optional-dependencies].dev`).

- [ ] **Step 2: Update `host-sidecar/pyproject.toml` to add pytest-asyncio dev dep**

In the `[project.optional-dependencies]` block:

```toml
dev = [
    "pytest==9.0.3",
    "pytest-asyncio==0.24.0",
    "httpx==0.27.2",
]
```

Also add to a new `[tool.pytest.ini_options]` section at the bottom:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 3: Run tests to verify failures**

```bash
cd host-sidecar && python -m pytest tests/test_tts.py -v 2>&1 | /usr/bin/tail -n 10
```
Expected: FAIL on `/tts` endpoint not existing (404 or import error).

- [ ] **Step 4: Implement `host-sidecar/jarvis_sidecar/tts.py`**

```python
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
```

- [ ] **Step 5: Wire `/tts` into `host-sidecar/jarvis_sidecar/app.py`**

Add inside `create_app`, after the `/health` route:

```python
    from pydantic import BaseModel
    from fastapi import Response
    from .tts import synthesize, TTSError

    class _TTSBody(BaseModel):
        text: str
        voice: str = "Alex"

    @app.post("/tts")
    async def tts(body: _TTSBody, _=Depends(require_token)) -> Response:
        try:
            audio = await synthesize(body.text, body.voice)
        except TTSError as e:
            msg = str(e)
            if "empty" in msg.lower():
                raise HTTPException(status_code=400, detail=msg)
            raise HTTPException(status_code=500, detail=msg)
        return Response(content=audio, media_type="audio/m4a")
```

- [ ] **Step 6: Re-run all sidecar tests**

```bash
cd host-sidecar && python -m pytest -v 2>&1 | /usr/bin/tail -n 15
```
Expected: 11 PASSED (4 auth + 3 health + 4 tts).

- [ ] **Step 7: `code-reviewer` persona pass on the diff**

Per CLAUDE.md routing — this commit adds a subprocess-spawning endpoint with auth. Dispatch `code-reviewer` persona on the diff. Apply must-fix.

- [ ] **Step 8: Commit**

```bash
git add host-sidecar/
git commit -m "feat(sidecar): /tts endpoint wraps macOS say with argv-safe invocation

DEVNULL on both stdout and stderr (pipe-deadlock defense). Voice + text
pass after \`--\` separator. Temp file cleaned in finally. Empty input
returns 400; subprocess failures return 500."
```

---

## Task 4: `/stt` endpoint (wraps `whisper-cli`)

**Files:**
- Create: `host-sidecar/jarvis_sidecar/stt.py`
- Modify: `host-sidecar/jarvis_sidecar/app.py`
- Create: `host-sidecar/tests/test_stt.py`

**Parallelism:** `[SEQUENTIAL after T3]`.

- [ ] **Step 1: Write failing test**

Create `host-sidecar/tests/test_stt.py`:

```python
"""
Hermetic /stt tests — mocks subprocess so neither ffmpeg nor whisper-cli
need to exist on the test runner.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = "test-token-12345"
HEADERS = {"X-SIDECAR-Token": TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    (tmp_path / "token").write_text(TOKEN, encoding="utf-8")
    for mod in list(sys.modules):
        if mod.startswith("jarvis_sidecar"):
            del sys.modules[mod]
    from jarvis_sidecar.app import create_app
    return TestClient(create_app()), tmp_path


class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


async def test_stt_happy_path(client, monkeypatch):
    c, _ = client
    captured = []

    async def fake_exec(*argv, **kwargs):
        captured.append(list(argv))
        if argv[0].endswith("ffmpeg"):
            # ffmpeg call — touch the output WAV.
            outpath = pathlib.Path(argv[argv.index("-i") + 2])
            outpath.write_bytes(b"FAKE-WAV-PCM")
        else:
            # whisper-cli call. -of <prefix> means it writes <prefix>.txt.
            of_idx = argv.index("-of")
            prefix = pathlib.Path(argv[of_idx + 1])
            (prefix.with_suffix(".txt")).write_text("hello world\n", encoding="utf-8")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    audio_blob = b"WEBM-OPUS-FAKE-BLOB"
    files = {"audio": ("clip.webm", BytesIO(audio_blob), "audio/webm")}
    r = c.post("/stt", files=files, headers=HEADERS)

    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "hello world"
    assert body["duration_ms"] >= 0

    # Two subprocess calls: ffmpeg, then whisper-cli.
    assert len(captured) == 2
    assert "ffmpeg" in captured[0][0]
    assert "-ar" in captured[0] and "16000" in captured[0]
    assert "-ac" in captured[0] and "1" in captured[0]
    assert captured[1][0].endswith("whisper-cli")
    assert "-nt" in captured[1] and "-np" in captured[1]
    assert "-otxt" in captured[1]
    # DEVNULL stdout discipline — checked indirectly: the test doesn't
    # capture any output piped from whisper-cli.


def test_stt_requires_token(client):
    c, _ = client
    files = {"audio": ("clip.webm", BytesIO(b"x"), "audio/webm")}
    r = c.post("/stt", files=files)
    assert r.status_code == 401


async def test_stt_whisper_nonzero_exit_returns_500(client, monkeypatch):
    c, _ = client

    async def fake_exec(*argv, **kwargs):
        if argv[0].endswith("ffmpeg"):
            outpath = pathlib.Path(argv[argv.index("-i") + 2])
            outpath.write_bytes(b"x")
            return _FakeProc(returncode=0)
        return _FakeProc(returncode=2)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    files = {"audio": ("clip.webm", BytesIO(b"x"), "audio/webm")}
    r = c.post("/stt", files=files, headers=HEADERS)
    assert r.status_code == 500


def test_stt_rejects_oversized_upload(client):
    """5 MiB cap defends against resource exhaustion."""
    c, _ = client
    big = b"x" * (5 * 1024 * 1024 + 1)
    files = {"audio": ("clip.webm", BytesIO(big), "audio/webm")}
    r = c.post("/stt", files=files, headers=HEADERS)
    assert r.status_code == 413
```

- [ ] **Step 2: Run to verify failures**

Expected: 4 FAILED (/stt doesn't exist).

- [ ] **Step 3: Implement `host-sidecar/jarvis_sidecar/stt.py`**

```python
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
```

- [ ] **Step 4: Wire `/stt` + size cap into `app.py`**

Add inside `create_app`, after the `/tts` route:

```python
    from fastapi import UploadFile, File
    from .stt import transcribe, STTError

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
```

- [ ] **Step 5: Run all sidecar tests**

```bash
cd host-sidecar && python -m pytest -v 2>&1 | /usr/bin/tail -n 20
```
Expected: 15 PASSED (4 auth + 3 health + 4 tts + 4 stt).

- [ ] **Step 6: `code-reviewer` persona pass**

This commit adds another subprocess endpoint + multipart upload handling. Dispatch `code-reviewer` on the diff.

- [ ] **Step 7: Commit**

```bash
git add host-sidecar/
git commit -m "feat(sidecar): /stt endpoint wraps ffmpeg + whisper-cli

Per spec §3.2 step 3 + security-advisor required fixes #3 and #4:
DEVNULL on both subprocess stdouts so transcript fragments never reach
the rotating log; mkdtemp with all three temp files cleaned in finally.
5 MiB upload cap (HTTP 413). Returns {text, duration_ms}."
```

---

## Task 5: `setup.sh` + `teardown.sh` + launchctl plist template

**Files:**
- Create: `host-sidecar/setup.sh`
- Create: `host-sidecar/teardown.sh`
- Create: `host-sidecar/com.jarvis.sidecar.plist`

**Parallelism:** `[SEQUENTIAL after T4]`.

- [ ] **Step 1: Write `host-sidecar/setup.sh`**

```bash
#!/usr/bin/env bash
# host-sidecar/setup.sh — one-shot installer for the jarvis-sidecar daemon.
#
# - brew installs whisper-cpp + ffmpeg (no-op if already present)
# - downloads ggml-base.en.bin to ~/Library/Application Support/jarvis-sidecar/models/
# - generates a token at ~/Library/Application Support/jarvis-sidecar/token (mode 600)
# - creates a python venv under host-sidecar/.venv and installs the package
# - renders + loads ~/Library/LaunchAgents/com.jarvis.sidecar.plist (KeepAlive: true)

set -euo pipefail

STATE_DIR="$HOME/Library/Application Support/jarvis-sidecar"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
PLIST_DST="$LAUNCHD_DIR/com.jarvis.sidecar.plist"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.jarvis.sidecar.plist"
VENV="$(cd "$(dirname "$0")" && pwd)/.venv"
SIDECAR_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[1/6] brew install whisper-cpp ffmpeg"
brew install whisper-cpp ffmpeg 2>&1 | tail -n 3 || true

echo "[2/6] state dir: $STATE_DIR"
mkdir -p "$STATE_DIR/models"
chmod 700 "$STATE_DIR"

echo "[3/6] downloading ggml-base.en.bin (~150 MB)"
MODEL_PATH="$STATE_DIR/models/ggml-base.en.bin"
if [[ ! -f "$MODEL_PATH" ]]; then
  curl -fL \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin" \
    -o "$MODEL_PATH"
else
  echo "    (model already present, skipping)"
fi

echo "[4/6] generating shared-secret token"
TOKEN_PATH="$STATE_DIR/token"
if [[ ! -f "$TOKEN_PATH" ]]; then
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$TOKEN_PATH"
  chmod 600 "$TOKEN_PATH"
  echo "    (new token written; mode 600)"
else
  echo "    (existing token preserved)"
fi

echo "[5/6] installing python venv + jarvis_sidecar"
python3.11 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$SIDECAR_DIR"

echo "[6/6] installing launchctl plist"
mkdir -p "$LAUNCHD_DIR"
# Render the plist with absolute paths inline (avoids env-var brittleness in launchd).
sed \
  -e "s|@@VENV@@|$VENV|g" \
  -e "s|@@HOME@@|$HOME|g" \
  "$PLIST_SRC" > "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo ""
echo "Done. Sidecar should be running on 127.0.0.1:9999."
echo "Token at: $TOKEN_PATH"
echo "Tail logs: tail -F \"$HOME/Library/Logs/jarvis-sidecar.log\""
```

- [ ] **Step 2: Write `host-sidecar/teardown.sh`**

```bash
#!/usr/bin/env bash
# host-sidecar/teardown.sh — uninstall.
#
# - unloads launchctl plist
# - removes ~/Library/LaunchAgents/com.jarvis.sidecar.plist
# - removes ~/Library/Application Support/jarvis-sidecar/ (token + models)
#
# Leaves the host-sidecar/.venv in place (cheap to recreate; user may want
# to re-install).

set -euo pipefail

STATE_DIR="$HOME/Library/Application Support/jarvis-sidecar"
PLIST_DST="$HOME/Library/LaunchAgents/com.jarvis.sidecar.plist"

if [[ -f "$PLIST_DST" ]]; then
  launchctl unload "$PLIST_DST" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "[1/2] plist unloaded and removed"
fi

if [[ -d "$STATE_DIR" ]]; then
  rm -rf "$STATE_DIR"
  echo "[2/2] state dir removed: $STATE_DIR"
fi

echo "Done. Sidecar uninstalled."
```

- [ ] **Step 3: Write `host-sidecar/com.jarvis.sidecar.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.jarvis.sidecar</string>

  <key>ProgramArguments</key>
  <array>
    <string>@@VENV@@/bin/python</string>
    <string>-m</string>
    <string>jarvis_sidecar</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardErrorPath</key>
  <string>@@HOME@@/Library/Logs/jarvis-sidecar.log</string>

  <key>StandardOutPath</key>
  <string>@@HOME@@/Library/Logs/jarvis-sidecar.log</string>

  <key>ProcessType</key>
  <string>Interactive</string>
</dict>
</plist>
```

- [ ] **Step 4: Make scripts executable**

```bash
chmod +x host-sidecar/setup.sh host-sidecar/teardown.sh
```

- [ ] **Step 5: Commit**

```bash
git add host-sidecar/setup.sh host-sidecar/teardown.sh host-sidecar/com.jarvis.sidecar.plist
git commit -m "feat(sidecar): setup.sh + teardown.sh + launchctl plist

Per spec §3.1 + security-advisor required fix #4 (teardown is blocking
for ship). KeepAlive: true so the sidecar survives crashes (spec §11
recommendation #2). Plist uses absolute paths (no shell env coupling)."
```

---

## Task 6: JARVIS backend — `sidecar_client.py`

**Files:**
- Create: `sidecar_client.py` (repo root, alongside server.py)
- Create: `tests/test_sidecar_client.py`

**Parallelism:** `[PARALLEL-OK with T5]` — different files, different concerns.

- [ ] **Step 1: Write failing test**

Create `tests/test_sidecar_client.py`:

```python
"""
Hermetic tests for sidecar_client — mocks httpx.AsyncClient so the tests
don't need a live sidecar.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sidecar_client


@pytest.fixture
def isolated_token(tmp_path, monkeypatch):
    """Bind-mount path for the token. Production lives at
    /host-sidecar-config/token (mounted from the host); tests stub the path."""
    monkeypatch.setattr(sidecar_client, "_TOKEN_PATH", tmp_path / "token")
    (tmp_path / "token").write_text("test-token-xyz", encoding="utf-8")
    yield


class _FakeResp:
    def __init__(self, status_code: int, content: bytes = b"", payload: dict | None = None):
        self.status_code = status_code
        self.content = content
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, headers=None):
        self.calls.append(("GET", url, headers, None))
        return self._responses.pop(0)

    async def post(self, url, headers=None, json=None, files=None, content=None):
        self.calls.append(("POST", url, headers, json or files or content))
        return self._responses.pop(0)


async def test_tts_via_sidecar_happy_path(isolated_token, monkeypatch):
    fake = _FakeClient(_FakeResp(200, content=b"AAC-FAKE"))
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: fake)
    audio = await sidecar_client.tts_via_sidecar("hello", voice="Alex")
    assert audio == b"AAC-FAKE"
    method, url, headers, body = fake.calls[0]
    assert method == "POST"
    assert url.endswith("/tts")
    assert headers["X-SIDECAR-Token"] == "test-token-xyz"
    assert body == {"text": "hello", "voice": "Alex"}


async def test_tts_via_sidecar_returns_none_on_connection_error(isolated_token, monkeypatch):
    class _Boom:
        async def __aenter__(self): raise sidecar_client.httpx.ConnectError("nope")
        async def __aexit__(self, *e): pass
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: _Boom())
    audio = await sidecar_client.tts_via_sidecar("hello", voice="Alex")
    assert audio is None


async def test_stt_via_sidecar_happy_path(isolated_token, monkeypatch):
    fake = _FakeClient(_FakeResp(200, payload={"text": "transcribed", "duration_ms": 350}))
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: fake)
    text = await sidecar_client.stt_via_sidecar(b"AUDIO-BYTES", mime_type="audio/webm")
    assert text == "transcribed"
    method, url, headers, body = fake.calls[0]
    assert method == "POST"
    assert url.endswith("/stt")
    assert headers["X-SIDECAR-Token"] == "test-token-xyz"


async def test_stt_via_sidecar_returns_empty_on_error(isolated_token, monkeypatch):
    fake = _FakeClient(_FakeResp(500, payload={"detail": "whisper-cli exited 2"}))
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: fake)
    text = await sidecar_client.stt_via_sidecar(b"x", mime_type="audio/webm")
    assert text == ""


async def test_sidecar_health(isolated_token, monkeypatch):
    fake = _FakeClient(_FakeResp(200, payload={"status": "ok", "say_available": True}))
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: fake)
    health = await sidecar_client.sidecar_health()
    assert health == {"status": "ok", "say_available": True}


async def test_sidecar_health_none_on_connection_error(isolated_token, monkeypatch):
    class _Boom:
        async def __aenter__(self): raise sidecar_client.httpx.ConnectError("nope")
        async def __aexit__(self, *e): pass
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: _Boom())
    health = await sidecar_client.sidecar_health()
    assert health is None


def test_token_missing_returns_empty():
    # Module-level lookup with no token file. Should not raise; just empty.
    import importlib
    import sidecar_client as sc
    sc._TOKEN_PATH = pathlib.Path("/nonexistent/path")
    importlib.reload(sc)  # forces re-init if module caches
    # The async calls would fail to send if token is empty; the helper guards.
    assert sc._read_token() == ""
```

- [ ] **Step 2: Run to verify failures**

```bash
docker run --rm --entrypoint="" -v "$PWD:/app" -w /app jarvis-backend:local python -m pytest tests/test_sidecar_client.py -v 2>&1 | /usr/bin/tail -n 15
```
Expected: FAIL (sidecar_client doesn't exist).

- [ ] **Step 3: Implement `sidecar_client.py` at repo root**

```python
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
```

- [ ] **Step 4: Run tests — expect 7 PASSED**

```bash
docker run --rm --entrypoint="" -v "$PWD:/app" -w /app jarvis-backend:local python -m pytest tests/test_sidecar_client.py -v 2>&1 | /usr/bin/tail -n 15
```

- [ ] **Step 5: Commit**

```bash
git add sidecar_client.py tests/test_sidecar_client.py
git commit -m "feat(server): sidecar_client — TTS/STT/health calls to host sidecar

X-SIDECAR-Token from bind-mounted file. All three calls degrade gracefully
on connection error (None / empty string). Module comment makes the
'transcript flows through same voice handler as Web Speech' invariant
explicit per security-advisor required fix #5."
```

---

## Task 7: Extend `synthesize_speech` with `sidecar` provider

**Files:**
- Modify: `server.py` (synthesize_speech function)
- Modify: `tests/test_server_locked_state.py` (add provider=sidecar test)

**Parallelism:** `[SEQUENTIAL after T6]`.

- [ ] **Step 1: Write failing test**

Append to `tests/test_server_locked_state.py`:

```python
async def test_synthesize_speech_uses_sidecar_when_configured(isolated_vault, monkeypatch):
    """TTS_PROVIDER=sidecar routes through sidecar_client (not Fish, not local)."""
    import server, sidecar_client
    isolated_vault.bootstrap("pp")
    isolated_vault.unlock("pp")
    sess = isolated_vault.session()
    sess.settings.set("TTS_PROVIDER", "sidecar")

    async def fake_tts(text, voice="Alex"):
        return b"SIDECAR-AUDIO"
    monkeypatch.setattr(sidecar_client, "tts_via_sidecar", fake_tts)

    audio = await server.synthesize_speech("hello")
    assert audio == b"SIDECAR-AUDIO"


async def test_synthesize_speech_auto_falls_through_to_sidecar(isolated_vault, monkeypatch):
    """TTS_PROVIDER=auto on a host where local say is NOT available should
    try the sidecar before falling back to Fish."""
    import server, sidecar_client
    from openclaw_ports import tts_local_cli
    isolated_vault.bootstrap("pp")
    isolated_vault.unlock("pp")
    sess = isolated_vault.session()
    sess.settings.set("TTS_PROVIDER", "auto")
    # No FISH_API_KEY set; sidecar must be the next thing tried.

    monkeypatch.setattr(tts_local_cli, "is_available", lambda: False)
    async def fake_tts(text, voice="Alex"):
        return b"SIDECAR-AUDIO"
    monkeypatch.setattr(sidecar_client, "tts_via_sidecar", fake_tts)

    audio = await server.synthesize_speech("hello")
    assert audio == b"SIDECAR-AUDIO"
```

- [ ] **Step 2: Verify the tests fail**

Existing `synthesize_speech` returns None when neither local say nor Fish key is configured. The new tests fail because there's no sidecar branch yet.

- [ ] **Step 3: Modify `synthesize_speech` in `server.py`**

Find the existing function (currently has `auto`, `local_cli`, `fish_audio` branches from PR #15). Replace with:

```python
async def synthesize_speech(text: str) -> Optional[bytes]:
    """Generate speech audio from text.

    Provider chosen by vault key `TTS_PROVIDER`:
      - "auto": try local `say` (host-only) → sidecar (Docker) → Fish (cloud)
      - "local_cli": local say only; None on failure
      - "sidecar": sidecar only; None on failure
      - "fish_audio": Fish only
    """
    from openclaw_ports import tts_local_cli
    import sidecar_client

    provider = (_vault_get("TTS_PROVIDER", "auto") or "auto").strip().lower()
    voice = _vault_get("TTS_VOICE", "Alex") or "Alex"

    # Local CLI path (only viable on macOS host install).
    if provider in ("auto", "local_cli") and tts_local_cli.is_available():
        try:
            audio = await tts_local_cli.synthesize(text, voice=voice)
            _session_tokens["tts_calls"] += 1
            _append_usage_entry(0, 0, "tts")
            return audio
        except tts_local_cli.CLITTSError as e:
            log.warning("local TTS failed: %s", e)
            if provider == "local_cli":
                return None
    elif provider == "local_cli":
        log.warning("TTS_PROVIDER=local_cli but local TTS unavailable; no audio")
        return None

    # Sidecar path (Docker host with the host-sidecar daemon running).
    if provider in ("auto", "sidecar"):
        audio = await sidecar_client.tts_via_sidecar(text, voice=voice)
        if audio is not None:
            _session_tokens["tts_calls"] += 1
            _append_usage_entry(0, 0, "tts")
            return audio
        if provider == "sidecar":
            return None
        # auto: fall through to Fish.

    # Fish Audio path (existing, unchanged).
    fish_api_key = _vault_get("FISH_API_KEY")
    fish_voice_id = _vault_get("FISH_VOICE_ID", "612b878b113047d9a770c069c8b4fdfe")
    if not fish_api_key:
        log.warning("FISH_API_KEY not set, skipping TTS")
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(
                FISH_API_URL,
                headers={"Authorization": f"Bearer {fish_api_key}", "Content-Type": "application/json"},
                json={"text": text, "reference_id": fish_voice_id, "format": "mp3"},
            )
        if response.status_code == 200:
            _session_tokens["tts_calls"] += 1
            _append_usage_entry(0, 0, "tts")
            return response.content
        log.error(f"TTS error: {response.status_code}")
        return None
    except Exception as e:
        log.error(f"TTS error: {e}")
        return None
```

- [ ] **Step 4: Run the new tests**

```bash
docker run --rm --entrypoint="" -v "$PWD:/app" -w /app jarvis-backend:local python -m pytest tests/test_server_locked_state.py::test_synthesize_speech_uses_sidecar_when_configured tests/test_server_locked_state.py::test_synthesize_speech_auto_falls_through_to_sidecar -v 2>&1 | /usr/bin/tail -n 15
```
Expected: 2 PASSED.

- [ ] **Step 5: Code-reviewer pass on `server.py` diff**

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_server_locked_state.py
git commit -m "feat(server): TTS provider \`sidecar\` added; auto-mode tries it before Fish"
```

---

## Task 8: New endpoint `POST /api/stt` + audit log

**Files:**
- Modify: `server.py` (add /api/stt route)
- Modify: `tests/test_server_locked_state.py`

**Parallelism:** `[SEQUENTIAL after T7]`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_server_locked_state.py`:

```python
def test_api_stt_returns_transcript(isolated_vault, monkeypatch):
    """POST /api/stt proxies to sidecar_client.stt_via_sidecar and returns
    {text: ...}."""
    import server, sidecar_client
    isolated_vault.bootstrap("pp")
    isolated_vault.unlock("pp")
    sess = isolated_vault.session()
    # Get the auth token; client must send it on /api/stt.
    token_resp = TestClient(server.app).post("/api/auth/unlock", json={"passphrase": "pp"})
    auth_token = token_resp.json()["token"]
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)

    async def fake_stt(audio_bytes, mime_type="audio/webm"):
        return "hello world"
    monkeypatch.setattr(sidecar_client, "stt_via_sidecar", fake_stt)

    c = TestClient(server.app)
    files = {"audio": ("clip.webm", b"WEBM-BLOB", "audio/webm")}
    r = c.post("/api/stt", files=files, headers={"X-JARVIS-Token": auth_token})
    assert r.status_code == 200
    assert r.json() == {"text": "hello world"}


def test_api_stt_requires_unlock_and_token(isolated_vault):
    """Locked vault returns 423; unlocked + missing token returns 401."""
    import server
    c = TestClient(server.app)
    files = {"audio": ("clip.webm", b"x", "audio/webm")}
    r = c.post("/api/stt", files=files)
    # Locked OR missing-token both blocked; we accept either status.
    assert r.status_code in (401, 423)


def test_api_stt_audit_log_entry_written(isolated_vault, monkeypatch, tmp_path):
    """Per security-advisor required fix #2: each /api/stt call appends an
    entry to data/audit.jsonl with timestamp, bytes, transcript_returned."""
    import server, sidecar_client, audit_log
    isolated_vault.bootstrap("pp")
    isolated_vault.unlock("pp")
    sess = isolated_vault.session()

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_PATH", audit_path)

    token_resp = TestClient(server.app).post("/api/auth/unlock", json={"passphrase": "pp"})
    auth_token = token_resp.json()["token"]
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)

    async def fake_stt(audio_bytes, mime_type="audio/webm"):
        return "hello"
    monkeypatch.setattr(sidecar_client, "stt_via_sidecar", fake_stt)

    c = TestClient(server.app)
    files = {"audio": ("clip.webm", b"WEBM-BLOB-12345", "audio/webm")}
    c.post("/api/stt", files=files, headers={"X-JARVIS-Token": auth_token})

    log_lines = audit_path.read_text().splitlines()
    assert any(
        '"kind": "stt_request"' in line and '"bytes": 15' in line and '"transcript_returned": true' in line
        for line in log_lines
    )
```

NOTE: this test assumes `audit_log` module exists. Verify by `grep -n "audit_log\|AUDIT_PATH" *.py`. If the existing audit module has a different shape, adapt the monkeypatch + assertion accordingly. Don't invent — read the module first.

- [ ] **Step 2: Run to verify failures**

- [ ] **Step 3: Implement `/api/stt` in `server.py`**

Find a good spot for the new route (near other `/api/*` endpoints, e.g. after `/api/settings/keys`). Add:

```python
from fastapi import UploadFile, File

@app.post("/api/stt")
async def api_stt(audio: UploadFile = File(...), request: Request = None) -> dict:
    """Speech-to-text via the host sidecar. Replaces Chrome Web Speech for
    privacy when STT_PROVIDER=whisper.
    """
    import sidecar_client
    contents = await audio.read()

    # Audit log (security-advisor required fix #2): metadata only, never the
    # transcript text. transcript_returned is bool, not the string.
    transcript = await sidecar_client.stt_via_sidecar(
        contents, mime_type=audio.content_type or "audio/webm"
    )

    try:
        from audit_log import audit_event
        audit_event(
            kind="stt_request",
            ip=request.client.host if request and request.client else "",
            bytes=len(contents),
            transcript_returned=bool(transcript),
        )
    except Exception:
        pass  # audit failures must not break user-facing requests

    return {"text": transcript}
```

If the existing audit-log module signature differs from `audit_event(kind, ip, bytes, transcript_returned)`, adapt — read `audit_log.py` first and use its actual public API.

- [ ] **Step 4: Run all server tests**

```bash
docker run --rm --entrypoint="" -v "$PWD:/app" -w /app jarvis-backend:local python -m pytest tests/test_server_locked_state.py tests/test_sidecar_client.py -v 2>&1 | /usr/bin/tail -n 25
```
Expected: all PASS.

- [ ] **Step 5: Code-reviewer pass — this is a security-touching endpoint (audit log + new ingress point)**

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_server_locked_state.py
git commit -m "feat(server): POST /api/stt + audit log entry per request

Per spec §4.3 + security-advisor required fix #2. Audit log records
metadata only (timestamp, IP, bytes, transcript_returned bool) —
never the transcript TEXT itself, which is voice PII. Audit failures
don't break the request."
```

---

## Task 9: Docker compose changes

**Files:**
- Modify: `docker-compose.yml`

**Parallelism:** `[PARALLEL-OK with T8 and earlier]`.

- [ ] **Step 1: Edit `docker-compose.yml`**

Find the `services.backend.volumes` block. Add ONE new line (the token file only — security-advisor required fix #1):

```yaml
    volumes:
      - ./data:/app/data:rw
      - ~/Library/Application Support/jarvis-sidecar/token:/host-sidecar-config/token:ro
```

Add `extra_hosts` if not already present:

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

- [ ] **Step 2: Verify compose parses**

```bash
docker compose -p jarvis config 2>&1 | /usr/bin/tail -n 30
```
Expected: no errors. `volumes` block shows the new bind; `extra_hosts` shows host-gateway.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(docker): bind-mount sidecar token + extra_hosts host.docker.internal"
```

---

## Task 10: Frontend STT path + settings UI

**Files:**
- Create: `frontend/src/stt.ts`
- Modify: `frontend/src/main.ts`
- Modify: `frontend/src/voice.ts`
- Modify: `frontend/src/settings.ts`

**Parallelism:** `[SEQUENTIAL after T8]`.

- [ ] **Step 1: Create `frontend/src/stt.ts`**

```ts
/**
 * Browser-side STT: record audio with MediaRecorder, POST the blob to
 * /api/stt, return the transcript.
 *
 * Used when vault key STT_PROVIDER === "whisper". Otherwise voice.ts falls
 * back to the existing Web Speech API path.
 */

import { withAuthHeaders } from "./auth-token";

export interface RecordingSession {
  stop(): Promise<string>;
  cancel(): void;
}

export async function startRecording(): Promise<RecordingSession> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const recorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
  const chunks: BlobPart[] = [];
  recorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) chunks.push(e.data);
  };
  recorder.start();

  let cancelled = false;

  return {
    async stop(): Promise<string> {
      return new Promise<string>((resolve, reject) => {
        recorder.onstop = async () => {
          stream.getTracks().forEach((t) => t.stop());
          if (cancelled) return resolve("");
          const blob = new Blob(chunks, { type: "audio/webm" });
          const form = new FormData();
          form.append("audio", blob, "clip.webm");
          try {
            const r = await fetch("/api/stt", withAuthHeaders({ method: "POST", body: form }));
            if (!r.ok) return reject(new Error(`HTTP ${r.status}`));
            const body = (await r.json()) as { text: string };
            resolve(body.text || "");
          } catch (err) {
            reject(err);
          }
        };
        recorder.stop();
      });
    },
    cancel() {
      cancelled = true;
      try { recorder.stop(); } catch {}
      stream.getTracks().forEach((t) => t.stop());
    },
  };
}
```

- [ ] **Step 2: Wire mode switch in `main.ts`**

Find the voice-input wiring. Add a new STT mode controlled by a vault-backed preference. Read the current `STT_PROVIDER` on boot from settings; default to `web_speech`. When `whisper`, the mic button toggles record/stop instead of always-listening:

```ts
import { startRecording, type RecordingSession } from "./stt";

let sttMode: "web_speech" | "whisper" = "web_speech";
let activeRecording: RecordingSession | null = null;

// Hydrate from settings on boot — fetch the prefs endpoint (already exists).
async function loadSttPref() {
  try {
    const r = await fetch("/api/settings/preferences", withAuthHeaders());
    const prefs = await r.json();
    if (prefs.stt_provider === "whisper") sttMode = "whisper";
  } catch { /* default web_speech */ }
}
loadSttPref();

// Update the mic button handler — when sttMode === "whisper", clicking
// starts/stops a recording instead of toggling listening:
btnMute.addEventListener("click", async (e) => {
  e.stopPropagation();
  if (sttMode === "whisper") {
    if (activeRecording) {
      const text = await activeRecording.stop();
      activeRecording = null;
      btnMute.classList.remove("recording");
      if (text) {
        socket.send({ type: "transcript", text, isFinal: true });
        pushUserLine(text);
        transition("thinking");
      } else {
        transition("idle");
      }
      return;
    }
    activeRecording = await startRecording();
    btnMute.classList.add("recording");
    transition("listening");
    return;
  }
  // Existing Web Speech path (untouched):
  isMuted = !isMuted;
  // ... rest of existing handler
});
```

The implementer should adapt the existing `btnMute.addEventListener` body to branch on `sttMode` at the top. Do not duplicate the Web Speech logic — branch and `return` from the whisper path.

- [ ] **Step 3: Settings UI — STT Provider dropdown**

In `frontend/src/settings.ts`:

Extend `PreferencesResponse`:
```ts
interface PreferencesResponse {
  // ... existing fields ...
  stt_provider?: string;  // "web_speech" | "whisper"
}
```

Add a new row in the API Keys (or User Preferences) section, mirroring the TTS_PROVIDER dropdown:

```html
<div class="settings-field">
  <label>STT Provider</label>
  <div class="settings-input-row">
    <select id="input-stt-provider">
      <option value="web_speech">Web Speech (browser → Google)</option>
      <option value="whisper">Whisper (local host sidecar)</option>
    </select>
    <button class="settings-btn" id="btn-save-stt-provider">Save</button>
  </div>
</div>
```

Save handler:
```ts
document.getElementById("btn-save-stt-provider")?.addEventListener("click", async () => {
  const value = (document.getElementById("input-stt-provider") as HTMLSelectElement).value;
  await apiPost("/api/settings/keys", { key_name: "STT_PROVIDER", key_value: value });
});
```

loadPreferences should hydrate the dropdown:
```ts
const sttEl = document.getElementById("input-stt-provider") as HTMLSelectElement;
if (sttEl) sttEl.value = prefs.stt_provider || "web_speech";
```

- [ ] **Step 4: Add `STT_PROVIDER` and `SIDECAR_URL` to the vault allowlist in `server.py`**

In `api_settings_keys`, extend the allowed set:

```python
allowed = {
    "ANTHROPIC_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID",
    "TTS_PROVIDER", "TTS_VOICE",
    "STT_PROVIDER", "SIDECAR_URL",  # NEW
    "GITHUB_TOKEN",
    "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS",
}
```

Add `stt_provider` to the `/api/settings/preferences` GET response (mirror how `tts_provider` is exposed today).

- [ ] **Step 5: Build the frontend**

```bash
cd frontend && npm run build 2>&1 | /usr/bin/tail -n 5
```
Expected: 0 TS errors.

- [ ] **Step 6: Code-reviewer pass on the combined frontend + server diff**

- [ ] **Step 7: Commit**

```bash
git add frontend/src/stt.ts frontend/src/main.ts frontend/src/voice.ts frontend/src/settings.ts server.py
git commit -m "feat(frontend): Whisper STT mode + settings dropdown

STT_PROVIDER ∈ {web_speech, whisper}. When whisper, the mic button
toggles record/stop and POSTs the blob to /api/stt. Existing Web
Speech path untouched. SIDECAR_URL also added to the vault allowlist."
```

---

## Task 11: Documentation updates

**Files:**
- Modify: `SECURITY.md` (membrane — tripwire will fire)
- Modify: `ARCHITECTURE.md` (membrane — tripwire will fire)
- Modify: `docs/DOCKER.md`
- Modify: `CLAUDE.md` (persona routing)
- Modify: `docs/BACKLOG.md`

**Parallelism:** `[PARALLEL-OK after T9 or T10]`.

- [ ] **Step 1: SECURITY.md updates**

Per spec §9 + security-advisor drift section. Add:
- Trust boundary row: `JARVIS Docker ↔ host sidecar` (loopback HTTP, X-SIDECAR-Token).
- Data-classification row for sidecar token: Secret, at-rest in `~/Library/Application Support/jarvis-sidecar/token` (mode 600).
- Data-classification row for voice audio: transient PII, never persisted by JARVIS or sidecar, temp files in macOS user-owned tmpdir cleaned on every request.
- "What is intentionally NOT defended against" section: another process running as the same macOS user can read the sidecar token file (chmod 600 + user separation is the only defense — same boundary as `data/secrets.db`).
- Update TTS egress paragraph: when `TTS_PROVIDER=sidecar` (or `auto` + sidecar available), no Fish Audio egress.
- Update STT line: when `STT_PROVIDER=whisper`, audio never leaves the local machine; replaces Chrome Web Speech ↔ Google.

- [ ] **Step 2: ARCHITECTURE.md updates**

- New module-map row: `host-sidecar/` — macOS host daemon exposing /tts and /stt; not part of the Docker image.
- New trust boundary #6: `Server → host sidecar`. HTTP loopback (`host.docker.internal:9999`), X-SIDECAR-Token authenticated.
- Update voice-loop sequence: branch at step 5 for `STT_PROVIDER=whisper` (audio → /api/stt → sidecar) and at the `synthesize_speech` line for `TTS_PROVIDER=sidecar`.

- [ ] **Step 3: docs/DOCKER.md updates**

Add a new section "Optional host sidecar for local TTS/STT". Include:
- One-line summary of why (kills Fish Audio + Google egress)
- `./host-sidecar/setup.sh` to install
- `./host-sidecar/teardown.sh` to uninstall (run BEFORE `docker compose -p jarvis down -v`)
- Egress allowlist update: when sidecar enabled, `host.docker.internal:9999` joins the allowed-egress list

- [ ] **Step 4: CLAUDE.md persona routing**

Find the Persona Routing table. Add a row:

```
| Editing any file under `host-sidecar/` | `security-advisor` reviews — new daemon surface; verify auth invariants and DEVNULL discipline preserved. |
```

- [ ] **Step 5: docs/BACKLOG.md**

Move P3a + P13 from the Priority queue to Done (recent):

```markdown
- **P3a + P13 merged (PR ##XX):** jarvis-sidecar — combined macOS host daemon exposing /tts (wraps `say`) and /stt (wraps `whisper-cli`). Eliminates the last two cloud voice egresses (Google Web Speech for STT, Fish Audio for TTS when in Docker). New vault keys: `STT_PROVIDER`, `SIDECAR_URL`, plus `TTS_PROVIDER=sidecar` value. Sidecar installs via `host-sidecar/setup.sh` + launchctl.
```

- [ ] **Step 6: Commit (tripwire on SECURITY.md + ARCHITECTURE.md will fire — expected)**

```bash
git add SECURITY.md ARCHITECTURE.md docs/DOCKER.md CLAUDE.md docs/BACKLOG.md
git commit -m "docs: jarvis-sidecar — security model, architecture, docker, backlog

SECURITY.md: new trust boundary + data classification rows.
ARCHITECTURE.md: host-sidecar/ in module map + new boundary #6.
DOCKER.md: opt-in sidecar section + teardown order.
CLAUDE.md: routing for host-sidecar/ edits.
BACKLOG.md: P3a + P13 marked done."
```

---

## Task 12: Final acceptance + PR

**Files:** verification + push only.

**Parallelism:** `[SEQUENTIAL after T11]`.

- [ ] **Step 1: Full hermetic test suite (Docker)**

```bash
docker compose -p jarvis build 2>&1 | /usr/bin/tail -n 3
docker run --rm --entrypoint="" -v "$PWD:/app" -w /app jarvis-backend:local python -m pytest -q 2>&1 | /usr/bin/tail -n 25
```
Expected: 0 NEW failures. The 4 pre-existing failures (test_feedback_loop + test_personas_setup membrane-hook) remain.

- [ ] **Step 2: Sidecar test suite (host, not Docker)**

```bash
cd host-sidecar && python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest -v 2>&1 | /usr/bin/tail -n 20
```
Expected: 15 PASSED (4 auth + 3 health + 4 tts + 4 stt).

- [ ] **Step 3: test-runner persona pass**

Per CLAUDE.md routing. Dispatch the persona; report exit codes verbatim.

- [ ] **Step 4: Live end-to-end smoke (host required)**

Manual steps; document outcome in PR description:

1. `./host-sidecar/setup.sh` — installs brew deps, downloads model, generates token, loads plist.
2. `curl -s -H "X-SIDECAR-Token: $(cat ~/Library/Application\ Support/jarvis-sidecar/token)" http://127.0.0.1:9999/health` — expect `{"status":"ok","whisper_model":"ggml-base.en.bin","say_available":true}`.
3. `docker compose -p jarvis up -d --wait` — JARVIS comes up.
4. In settings UI: set `TTS_PROVIDER=sidecar`, `STT_PROVIDER=whisper`. Save.
5. Refresh page, unlock vault.
6. Click mic, speak "hello", click mic again. Transcript appears as USER line. JARVIS speaks back via the sidecar.

- [ ] **Step 5: code-reviewer persona on the full branch diff**

```bash
git diff main...HEAD --stat
```
Dispatch code-reviewer on the diff. Apply must-fix.

- [ ] **Step 6: Push + open PR**

```bash
git push -u origin feat/sidecar-implementation 2>&1 | /usr/bin/tail -n 5

gh pr create --title "feat: jarvis-sidecar — local TTS/STT host daemon (P3a + P13)" --body "$(cat <<'BODY'
Implements docs/superpowers/specs/2026-05-26-jarvis-sidecar-design.md.

## Summary
- New \`host-sidecar/\` Python/FastAPI daemon on \`127.0.0.1:9999\`
- \`POST /tts\` wraps macOS \`say\`; \`POST /stt\` wraps \`whisper-cli\`
- \`X-SIDECAR-Token\` auth; token bind-mounted RO into JARVIS Docker
- New TTS_PROVIDER=sidecar; new vault key STT_PROVIDER=whisper
- Audit log entry per /api/stt call (metadata only, no transcript text)
- launchctl-managed with KeepAlive: true; setup.sh / teardown.sh scripts

## Eliminates
- Chrome Web Speech → Google (when STT_PROVIDER=whisper)
- Fish Audio → fish.audio (when TTS_PROVIDER ∈ {auto, sidecar})

## Test plan
- [x] 15 hermetic sidecar tests (\`cd host-sidecar && pytest\`)
- [x] 7+ new JARVIS-backend tests covering sidecar_client + /api/stt
- [x] 0 new pytest regressions in the Docker suite
- [x] code-reviewer + test-runner persona passes
- [ ] Manual end-to-end smoke per Task 12 Step 4

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

- [ ] **Step 7: Wait for CI green + report**

```bash
gh pr checks
```

Do NOT auto-merge. User reviews.

---

## Self-review against the spec

**Spec coverage:**

| Spec section | Task(s) |
|---|---|
| §1 Goals | T1–T11 collectively |
| §2 Non-goals | Honored — no streaming, no other models, no Linux sidecar |
| §3.1 Sidecar service shape | T1, T5 |
| §3.2 Endpoints | T2 (/health), T3 (/tts), T4 (/stt) |
| §3.3 Auth model (shared token) | T2 |
| §4.1 sidecar_client module | T6 |
| §4.2 synthesize_speech dispatcher | T7 |
| §4.3 POST /api/stt + audit log | T8 |
| §5 Frontend STT + UI dropdown | T10 |
| §6 Docker compose bind mount + extra_hosts | T9 |
| §7 Trust boundary documentation | T11 (SECURITY.md update) |
| §8 Tests | Distributed across T2–T8, T10 |
| §9 Docs (SECURITY/ARCH/DOCKER/CLAUDE/BACKLOG) | T11 |
| §11 Open questions resolved (5 required fixes) | T6 (#5), T8 (#2), T4 (#3, #4), T9 (#1) |

**Placeholder scan:** No "TBD", no "implement later". Every step has executable code or commands.

**Type consistency:** `synthesize`, `transcribe`, `tts_via_sidecar`, `stt_via_sidecar`, `sidecar_health`, `_read_token`, `header_matches`, `_constant_time_equal`, `TTSError`, `STTError`, `HEADER_NAME = "X-SIDECAR-Token"`, `_TOKEN_PATH`, `_BASE_URL`, `STT_MAX_BYTES`, `SAY_TIMEOUT_S`, `WHISPER_TIMEOUT_S`, `DEFAULT_WHISPER_MODEL` — all defined in T1–T6 and used consistently downstream.

**Persona gates wired in:** code-reviewer at T3 (step 7), T4 (step 6), T7 (step 5), T8 (step 5), T10 (step 6), T12 (step 5). test-runner at T12 (step 3).

Plan ships as-is.

---

## Parallelism map (for subagent dispatch)

```
T1 (scaffold) ─► T2 (/health+auth) ─► T3 (/tts) ─► T4 (/stt) ─► T5 (setup.sh)
                                                                    │
                                                                    ▼
T6 (sidecar_client) ────────────────────────────────────► T7 (synth_speech)
                                                                    │
                                                                    ▼
                                                            T8 (/api/stt)
                                                                    │
                                                                    ▼
                                          T9 (docker compose) │ T10 (frontend)
                                                          │  │
                                                          ▼  ▼
                                                          T11 (docs)
                                                              │
                                                              ▼
                                                          T12 (PR)
```

T6 and T9 can run in parallel with T5 (different files). T10 can run in parallel with T11. Sequential implementer dispatch within a task. Total ~12 subagent dispatches + per-task reviews.
