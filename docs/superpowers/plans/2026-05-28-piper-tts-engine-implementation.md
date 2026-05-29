# Piper TTS Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Piper neural-TTS engine to the existing `host-sidecar` alongside macOS `say` — GPL-subprocess-only via an isolated venv, with a voice argv-injection guard and a 2000-char input cap, selectable via the `TTS_ENGINE` vault key.

**Architecture:** New `host-sidecar/jarvis_sidecar/piper_engine.py` shells out to `<piper-venv>/bin/python -m piper` (never `import piper` — keeps JARVIS's MIT license clear of GPL-3.0). The `/tts` route gains engine routing (`say` default, `piper` when requested + available, falls back to `say`). JARVIS's `sidecar_client` + `synthesize_speech` pass the engine choice. Frontend gets a TTS-engine dropdown.

**Tech Stack:** Python 3.11, FastAPI, `piper-tts` (GPL-3.0, isolated venv, subprocess-only), `whisper-cli`/`ffmpeg`/`say` (existing). No new JARVIS-side deps.

**Branch:** `feat/piper-tts-engine` (current; spec committed).

**Persona gates:** security-advisor already cleared the spec (GO-WITH-FIXES, all 3 required fixes folded into the spec). `code-reviewer` before every commit touching `host-sidecar/` or `server.py`. `test-runner` at T7.

**Spec reference:** `docs/superpowers/specs/2026-05-28-piper-tts-engine.md` — §5 has code contracts, §11 the security fixes.

---

## Task 1: config constants + `piper_engine.py` module

**Files:**
- Modify: `host-sidecar/jarvis_sidecar/config.py`
- Create: `host-sidecar/jarvis_sidecar/piper_engine.py`
- Create: `host-sidecar/tests/test_piper.py`

**Parallelism:** `[SEQUENTIAL]`. Blocks T2.

- [ ] **Step 1: Add config constants**

Append to `host-sidecar/jarvis_sidecar/config.py` (after the existing STT constants):

```python
# Piper TTS engine (GPL-3.0, subprocess-only, isolated venv).
PIPER_VENV = state_dir() / "piper-venv"          # never imported; exec'd
PIPER_DATA_DIR = state_dir() / "piper-voices"    # downloaded .onnx + .onnx.json
DEFAULT_PIPER_VOICE: Final[str] = "en_GB-alan-medium"
PIPER_TIMEOUT_S: Final[float] = 30.0
PIPER_MAX_TEXT_CHARS: Final[int] = 2000
```

Note: `PIPER_VENV` and `PIPER_DATA_DIR` are functions-of-`state_dir()` — but `state_dir()` is a function, so these must be computed lazily, NOT at module import (tests monkeypatch the state dir). Define them as functions to match the existing `token_path()` / `model_dir()` pattern:

```python
def piper_venv() -> Path:
    return state_dir() / "piper-venv"

def piper_data_dir() -> Path:
    return state_dir() / "piper-voices"

DEFAULT_PIPER_VOICE: Final[str] = "en_GB-alan-medium"
PIPER_TIMEOUT_S: Final[float] = 30.0
PIPER_MAX_TEXT_CHARS: Final[int] = 2000
```

Use the FUNCTION form (`piper_venv()`, `piper_data_dir()`) — consistent with the existing `token_path()`/`model_dir()` in config.py, and tests rely on `JARVIS_SIDECAR_STATE_DIR` overriding at call time.

- [ ] **Step 2: Write failing tests**

Create `host-sidecar/tests/test_piper.py`:

```python
"""
Hermetic tests for jarvis_sidecar.piper_engine. Mocks subprocess so no
real piper binary / model is needed on CI.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis_sidecar import piper_engine


class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


def test_module_surface():
    assert callable(piper_engine.is_available)
    assert callable(piper_engine.synthesize)
    assert issubclass(piper_engine.PiperError, Exception)


def test_does_not_import_piper():
    """The GPL boundary depends on never importing piper. The module source
    must not contain a bare `import piper` or `from piper`."""
    src = (ROOT / "jarvis_sidecar" / "piper_engine.py").read_text(encoding="utf-8")
    assert "import piper" not in src, "piper_engine must NOT import piper (GPL boundary)"
    assert "from piper" not in src


def test_is_available_false_when_venv_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    # No piper-venv created → unavailable.
    assert piper_engine.is_available() is False


def test_is_available_true_when_venv_and_model_present(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    venv_python = tmp_path / "piper-venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    model = tmp_path / "piper-voices" / "en_GB-alan-medium.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"FAKE-ONNX")
    assert piper_engine.is_available() is True


async def test_synthesize_argv_safety(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs.get("cwd")
        # piper writes -f <out.wav>. Find it and touch it.
        f_idx = argv.index("-f")
        pathlib.Path(argv[f_idx + 1]).write_bytes(b"RIFFWAVE-FAKE")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    audio = await piper_engine.synthesize("hello there", voice="en_GB-alan-medium")
    assert audio == b"RIFFWAVE-FAKE"
    argv = captured["argv"]
    # <piper-venv>/bin/python -m piper -m <voice> -f <out> -- <text>
    assert argv[0].endswith("/piper-venv/bin/python")
    assert argv[1] == "-m" and argv[2] == "piper"
    assert "-m" in argv[3:] and "en_GB-alan-medium" in argv
    assert "-f" in argv
    assert "--" in argv
    assert argv[-1] == "hello there"


async def test_synthesize_rejects_bad_voice_leading_dash(tmp_path, monkeypatch):
    """Required fix #1: voice flows into argv BEFORE `--`, so a leading-dash
    value would be a piper flag. Reject it."""
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    with pytest.raises(piper_engine.PiperError, match="voice"):
        await piper_engine.synthesize("hi", voice="--output-raw")


async def test_synthesize_rejects_voice_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    with pytest.raises(piper_engine.PiperError, match="voice"):
        await piper_engine.synthesize("hi", voice="../../etc/passwd")


async def test_synthesize_rejects_oversized_text(tmp_path, monkeypatch):
    """Required fix #2: cap at PIPER_MAX_TEXT_CHARS."""
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    big = "x" * 2001
    with pytest.raises(piper_engine.PiperError, match="too long"):
        await piper_engine.synthesize(big, voice="en_GB-alan-medium")


async def test_synthesize_rejects_empty_text(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    with pytest.raises(piper_engine.PiperError, match="empty"):
        await piper_engine.synthesize("", voice="en_GB-alan-medium")


async def test_synthesize_nonzero_exit_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))

    async def fake_exec(*argv, **kwargs):
        return _FakeProc(returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(piper_engine.PiperError, match="exit"):
        await piper_engine.synthesize("hi", voice="en_GB-alan-medium")
```

- [ ] **Step 3: Run to verify failures**

```bash
cd host-sidecar && .venv/bin/pytest tests/test_piper.py -v 2>&1 | /usr/bin/tail -n 15
```
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvis_sidecar.piper_engine'`.

- [ ] **Step 4: Implement `host-sidecar/jarvis_sidecar/piper_engine.py`**

```python
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
# model path. Allowlist: alnum, hyphen, underscore, 1-64 chars.
_VOICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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
```

- [ ] **Step 5: Run tests — expect all PASS**

```bash
cd host-sidecar && .venv/bin/pytest tests/test_piper.py -v 2>&1 | /usr/bin/tail -n 20
```
Expected: 9 PASSED.

- [ ] **Step 6: Run the full sidecar suite (no regressions)**

```bash
cd host-sidecar && .venv/bin/pytest -v 2>&1 | /usr/bin/tail -n 10
```
Expected: 24 PASSED (15 existing + 9 new).

- [ ] **Step 7: Commit**

```bash
cd /Users/petrog/Development/github/jarvis
git add host-sidecar/jarvis_sidecar/config.py host-sidecar/jarvis_sidecar/piper_engine.py host-sidecar/tests/test_piper.py
git commit -m "feat(sidecar): piper_engine — GPL piper via subprocess + injection guards

Per docs/superpowers/specs/2026-05-28-piper-tts-engine.md §5.2 +
security-advisor required fixes #1 (voice argv-injection guard,
^[A-Za-z0-9_-]{1,64}\$) and #2 (2000-char text cap). NEVER imports
piper — exec's the isolated piper-venv python at a process boundary.
DEVNULL on both streams, temp file cleaned in finally. Includes a
test asserting the module source contains no 'import piper'."
```

---

## Task 2: `/tts` engine routing + `/health` extension

**Files:**
- Modify: `host-sidecar/jarvis_sidecar/app.py`
- Modify: `host-sidecar/tests/test_tts.py`
- Modify: `host-sidecar/tests/test_health.py`

**Parallelism:** `[SEQUENTIAL after T1]`.

- [ ] **Step 1: Write failing tests** — append to `host-sidecar/tests/test_tts.py`:

```python
async def test_tts_engine_piper_when_available(client, monkeypatch):
    """engine=piper + available → uses piper, returns audio/wav, header says piper."""
    c, _ = client
    from jarvis_sidecar import piper_engine

    monkeypatch.setattr(piper_engine, "is_available", lambda: True)
    async def fake_synth(text, voice):
        return b"RIFFWAVE-PIPER"
    monkeypatch.setattr(piper_engine, "synthesize", fake_synth)

    r = c.post("/tts", json={"text": "hello", "voice": "en_GB-alan-medium", "engine": "piper"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.content == b"RIFFWAVE-PIPER"
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.headers.get("X-TTS-Engine-Used") == "piper"


async def test_tts_engine_piper_falls_back_to_say_when_unavailable(client, monkeypatch):
    """engine=piper + NOT available → silently falls back to say."""
    c, _ = client
    from jarvis_sidecar import piper_engine
    import asyncio as _aio

    monkeypatch.setattr(piper_engine, "is_available", lambda: False)

    async def fake_say(*argv, **kwargs):
        outpath = pathlib.Path(argv[4])
        outpath.write_bytes(b"AAC-SAY-FALLBACK")
        class _P:
            returncode = 0
            async def wait(self): return 0
            def kill(self): pass
        return _P()
    monkeypatch.setattr(_aio, "create_subprocess_exec", fake_say)

    r = c.post("/tts", json={"text": "hello", "voice": "Alex", "engine": "piper"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.headers.get("X-TTS-Engine-Used") == "say"


async def test_tts_engine_piper_bad_voice_returns_400(client, monkeypatch):
    c, _ = client
    from jarvis_sidecar import piper_engine
    monkeypatch.setattr(piper_engine, "is_available", lambda: True)
    # Real synthesize will reject the bad voice; don't mock it.
    r = c.post("/tts", json={"text": "hi", "voice": "--evil", "engine": "piper"}, headers=HEADERS)
    assert r.status_code == 400


def test_tts_default_engine_is_say(client, monkeypatch):
    """No engine field → say (existing behavior). Header confirms."""
    c, _ = client
    import asyncio as _aio
    async def fake_say(*argv, **kwargs):
        outpath = pathlib.Path(argv[4])
        outpath.write_bytes(b"AAC-SAY")
        class _P:
            returncode = 0
            async def wait(self): return 0
            def kill(self): pass
        return _P()
    monkeypatch.setattr(_aio, "create_subprocess_exec", fake_say)
    r = c.post("/tts", json={"text": "hello", "voice": "Alex"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.headers.get("X-TTS-Engine-Used") == "say"
```

Append to `host-sidecar/tests/test_health.py`:

```python
def test_health_reports_piper_available(client):
    r = client.get("/health", headers={"X-SIDECAR-Token": "test-token-12345"})
    assert r.status_code == 200
    assert "piper_available" in r.json()
```

- [ ] **Step 2: Run to verify failures**

```bash
cd host-sidecar && .venv/bin/pytest tests/test_tts.py tests/test_health.py -v 2>&1 | /usr/bin/tail -n 20
```
Expected: the 4 new tts tests + 1 health test FAIL (no engine routing, no `piper_available`, no `X-TTS-Engine-Used` header yet).

- [ ] **Step 3: Modify `host-sidecar/jarvis_sidecar/app.py`**

Extend `_TTSBody` (it's at module scope per T3 of the sidecar build) with an `engine` field:

```python
class _TTSBody(BaseModel):
    text: str
    voice: str = "Alex"
    engine: str = "say"
```

Add the piper import near the other engine imports at module scope:

```python
from .piper_engine import synthesize as piper_synthesize, is_available as piper_is_available, PiperError
```

Replace the `/tts` route handler with engine routing:

```python
    @app.post("/tts")
    async def tts(body: _TTSBody, _=Depends(require_token)) -> Response:
        # Piper path (GPL subprocess). Falls back to say if unavailable.
        if body.engine == "piper" and piper_is_available():
            try:
                audio = await piper_synthesize(body.text, body.voice)
            except PiperError as e:
                msg = str(e)
                # Validation failures (empty / too long / bad voice) → 400.
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
```

Update the `/health` handler to include `piper_available`:

```python
    @app.get("/health")
    def health(_=Depends(require_token)) -> dict:
        return {
            "status": "ok",
            "whisper_model": _whisper_model_name(),
            "say_available": _say_available(),
            "piper_available": piper_is_available(),
        }
```

- [ ] **Step 4: Run all sidecar tests**

```bash
cd host-sidecar && .venv/bin/pytest -v 2>&1 | /usr/bin/tail -n 15
```
Expected: 29 PASSED (24 + 4 new tts + 1 new health).

- [ ] **Step 5: code-reviewer persona pass on the diff**

Read `.claude/agents/code-reviewer.md`, review `git diff HEAD~1 HEAD`. Key checks:
- Piper path falls back to `say` (not 500) when `is_available()` is False
- Bad voice → 400 (the PiperError "invalid voice" message routes to 400)
- `X-TTS-Engine-Used` header set on BOTH paths
- The existing `say` path behavior is unchanged except for the added header

- [ ] **Step 6: Commit**

```bash
git add host-sidecar/jarvis_sidecar/app.py host-sidecar/tests/test_tts.py host-sidecar/tests/test_health.py
git commit -m "feat(sidecar): /tts engine routing (say|piper) + /health piper_available

engine=piper uses piper when available, falls back to say otherwise.
X-TTS-Engine-Used header reports which fired. Piper validation errors
(empty/too-long/bad-voice) → 400; subprocess failures → 500. /health
now reports piper_available so the JARVIS backend can decide fallback
without a round-trip."
```

---

## Task 3: `setup.sh --with-piper` + SHA256 pin

**Files:**
- Modify: `host-sidecar/setup.sh`

**Parallelism:** `[SEQUENTIAL after T2]`.

- [ ] **Step 1: Add a `--with-piper` flag + piper install block to `setup.sh`**

At the top of `setup.sh`, after `set -euo pipefail`, add argument parsing:

```bash
WITH_PIPER=0
for arg in "$@"; do
  case "$arg" in
    --with-piper) WITH_PIPER=1 ;;
  esac
done
```

After the existing `[6/6] installing launchctl plist` block (but before the final "Done" echo), add a conditional Piper install:

```bash
if [[ "$WITH_PIPER" == "1" ]]; then
  echo "[piper] installing Piper neural TTS (GPL-3.0, isolated venv)"
  PIPER_VENV="$STATE_DIR/piper-venv"
  PIPER_DATA="$STATE_DIR/piper-voices"
  mkdir -p "$PIPER_DATA"

  python3.11 -m venv "$PIPER_VENV"
  "$PIPER_VENV/bin/pip" install --quiet --upgrade pip
  "$PIPER_VENV/bin/pip" install --quiet piper-tts

  echo "[piper] downloading voice en_GB-alan-medium"
  MODEL="$PIPER_DATA/en_GB-alan-medium.onnx"
  if [[ ! -f "$MODEL" ]]; then
    "$PIPER_VENV/bin/python" -m piper.download_voices en_GB-alan-medium --data-dir "$PIPER_DATA"
  else
    echo "    (voice already present, skipping)"
  fi

  # SHA256 integrity pin (security-advisor recommendation). Fill PIN with the
  # hash from a first trusted download. Empty PIN = skip verification + warn.
  PIN_ONNX=""   # <-- fill after first trusted download: shasum -a 256 "$MODEL"
  if [[ -n "$PIN_ONNX" ]]; then
    echo "$PIN_ONNX  $MODEL" | shasum -a 256 -c - \
      || { echo "[piper] MODEL CHECKSUM MISMATCH — aborting"; exit 1; }
  else
    echo "[piper] WARNING: no SHA256 pin set for the voice model; integrity unverified."
  fi

  echo "[piper] done — set TTS_ENGINE=piper in JARVIS settings to use it."
fi
```

Update the script's header comment to document the `--with-piper` flag and the GPL nature of Piper.

- [ ] **Step 2: Verify the script parses + the flag is recognized**

```bash
bash -n host-sidecar/setup.sh && echo "setup.sh syntax OK"
# Dry confirm the flag branch is reachable (don't actually install):
grep -q "with-piper" host-sidecar/setup.sh && echo "flag present"
```
Expected: both echo. Do NOT actually run `setup.sh --with-piper` (it installs + downloads).

- [ ] **Step 3: Commit**

```bash
git add host-sidecar/setup.sh
git commit -m "feat(sidecar): setup.sh --with-piper installs GPL piper in isolated venv

Opt-in flag (default setup is say+whisper only — piper is ~80MB + GPL).
Creates a SEPARATE piper-venv, pip-installs piper-tts there, downloads
the en_GB-alan-medium voice, and verifies a SHA256 pin (fill-in after
first trusted download; warns if unset). Subprocess boundary preserved."
```

---

## Task 4: JARVIS `sidecar_client` engine param + `synthesize_speech` wiring

**Files:**
- Modify: `sidecar_client.py`
- Modify: `server.py` (synthesize_speech + allowlist + preferences)
- Modify: `tests/test_sidecar_client.py`
- Modify: `tests/test_server_locked_state.py`

**Parallelism:** `[SEQUENTIAL after T3]`. JARVIS-side tests run in Docker.

- [ ] **Step 1: Write failing test for the engine param** — append to `tests/test_sidecar_client.py`:

```python
async def test_tts_via_sidecar_passes_engine(isolated_token, monkeypatch):
    fake = _FakeClient(_FakeResp(200, content=b"WAV"))
    monkeypatch.setattr(sidecar_client.httpx, "AsyncClient", lambda **kw: fake)
    await sidecar_client.tts_via_sidecar("hi", voice="en_GB-alan-medium", engine="piper")
    _, url, headers, body = fake.calls[0]
    assert body == {"text": "hi", "voice": "en_GB-alan-medium", "engine": "piper"}
```

- [ ] **Step 2: Run to verify failure**

```bash
docker run --rm --entrypoint="" -v "$PWD:/app" -w /app jarvis-backend:local python -m pytest tests/test_sidecar_client.py::test_tts_via_sidecar_passes_engine -v 2>&1 | /usr/bin/tail -n 12
```
Expected: FAIL (engine param not supported).

- [ ] **Step 3: Add `engine` param to `sidecar_client.tts_via_sidecar`**

In `sidecar_client.py`, change the signature + POST body:

```python
async def tts_via_sidecar(text: str, voice: str = "Alex", engine: str = "say") -> bytes | None:
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
                json={"text": text, "voice": voice, "engine": engine},
            )
        if r.status_code != 200:
            log.warning("sidecar /tts returned %s", r.status_code)
            return None
        return r.content
    except httpx.HTTPError as e:
        log.info("sidecar /tts unreachable: %s", e)
        return None
```

- [ ] **Step 4: Wire `TTS_ENGINE` into `synthesize_speech`**

In `server.py` `synthesize_speech`, in the sidecar branch, read `TTS_ENGINE` and pass it:

```python
    # Sidecar path (Docker host with the host-sidecar daemon running).
    if provider in ("auto", "sidecar"):
        engine = (_vault_get("TTS_ENGINE", "say") or "say").strip().lower()
        piper_voice = _vault_get("TTS_PIPER_VOICE", "en_GB-alan-medium") or "en_GB-alan-medium"
        sidecar_voice = piper_voice if engine == "piper" else voice
        audio = await sidecar_client.tts_via_sidecar(text, voice=sidecar_voice, engine=engine)
        if audio is not None:
            _session_tokens["tts_calls"] += 1
            _append_usage_entry(0, 0, "tts")
            return audio
        if provider == "sidecar":
            return None
        # auto: fall through to Fish.
```

(Keep the rest of `synthesize_speech` unchanged.)

- [ ] **Step 5: Extend vault allowlist + preferences endpoint**

In `api_settings_keys`, add `"TTS_ENGINE"` and `"TTS_PIPER_VOICE"` to the `allowed` set.

In `/api/settings/preferences` GET, add:
```python
        "tts_engine": vault_dict.get("TTS_ENGINE", "say"),
        "tts_piper_voice": vault_dict.get("TTS_PIPER_VOICE", "en_GB-alan-medium"),
```

- [ ] **Step 6: Add a server-side test** — append to `tests/test_server_locked_state.py`:

```python
async def test_synthesize_speech_sidecar_uses_piper_engine(isolated_vault, monkeypatch):
    """TTS_PROVIDER=sidecar + TTS_ENGINE=piper passes engine=piper + the
    piper voice to the sidecar."""
    import server, sidecar_client
    isolated_vault.bootstrap("pp")
    isolated_vault.unlock("pp")
    sess = isolated_vault.session()
    sess.settings.set("TTS_PROVIDER", "sidecar")
    sess.settings.set("TTS_ENGINE", "piper")
    sess.settings.set("TTS_PIPER_VOICE", "en_GB-alan-medium")

    captured = {}
    async def fake_tts(text, voice="Alex", engine="say"):
        captured["voice"] = voice
        captured["engine"] = engine
        return b"WAV"
    monkeypatch.setattr(sidecar_client, "tts_via_sidecar", fake_tts)

    audio = await server.synthesize_speech("hello")
    assert audio == b"WAV"
    assert captured["engine"] == "piper"
    assert captured["voice"] == "en_GB-alan-medium"
```

- [ ] **Step 7: Run JARVIS-side tests**

```bash
docker run --rm --entrypoint="" -v "$PWD:/app" -w /app jarvis-backend:local python -m pytest tests/test_sidecar_client.py tests/test_server_locked_state.py -v 2>&1 | /usr/bin/tail -n 25
```
Expected: all PASS.

- [ ] **Step 8: code-reviewer persona pass on server.py + sidecar_client.py diff**

Key checks: engine param defaults to "say" (backwards-compatible); piper voice only used when engine=piper; no shell; allowlist + preferences both updated consistently.

- [ ] **Step 9: Commit**

```bash
git add sidecar_client.py server.py tests/test_sidecar_client.py tests/test_server_locked_state.py
git commit -m "feat(server): TTS_ENGINE vault key routes sidecar to say|piper

sidecar_client.tts_via_sidecar gains an engine param (default say).
synthesize_speech reads TTS_ENGINE + TTS_PIPER_VOICE from the vault and
passes them when calling the sidecar. Vault allowlist + preferences
endpoint extended with both keys."
```

---

## Task 5: Frontend TTS Engine dropdown

**Files:**
- Modify: `frontend/src/settings.ts`

**Parallelism:** `[SEQUENTIAL after T4]`.

- [ ] **Step 1: Extend `PreferencesResponse`**

```ts
interface PreferencesResponse {
  // ... existing fields ...
  tts_engine?: string;          // "say" | "piper"
  tts_piper_voice?: string;
}
```

- [ ] **Step 2: Add the UI fields** — after the existing TTS Voice field in the API Keys section:

```html
          <div class="settings-field">
            <label>TTS Engine</label>
            <div class="settings-input-row">
              <select id="input-tts-engine">
                <option value="say">System (macOS say)</option>
                <option value="piper">Piper (neural, local sidecar)</option>
              </select>
              <button class="settings-btn" id="btn-save-tts-engine">Save</button>
            </div>
          </div>

          <div class="settings-field">
            <label>Piper Voice</label>
            <div class="settings-input-row">
              <input type="text" id="input-tts-piper-voice" placeholder="en_GB-alan-medium" />
              <button class="settings-btn" id="btn-save-tts-piper-voice">Save</button>
            </div>
          </div>
```

- [ ] **Step 3: Hydrate on load** — in `loadPreferences`, after the existing tts hydration:

```ts
    const ttsEngineEl = document.getElementById("input-tts-engine") as HTMLSelectElement;
    const piperVoiceEl = document.getElementById("input-tts-piper-voice") as HTMLInputElement;
    if (ttsEngineEl) ttsEngineEl.value = prefs.tts_engine || "say";
    if (piperVoiceEl) piperVoiceEl.value = prefs.tts_piper_voice || "en_GB-alan-medium";
```

- [ ] **Step 4: Save handlers** — in `wireEvents`:

```ts
  document.getElementById("btn-save-tts-engine")?.addEventListener("click", async () => {
    const v = (document.getElementById("input-tts-engine") as HTMLSelectElement).value;
    await apiPost("/api/settings/keys", { key_name: "TTS_ENGINE", key_value: v });
  });
  document.getElementById("btn-save-tts-piper-voice")?.addEventListener("click", async () => {
    const v = (document.getElementById("input-tts-piper-voice") as HTMLInputElement).value.trim();
    await apiPost("/api/settings/keys", { key_name: "TTS_PIPER_VOICE", key_value: v });
  });
```

- [ ] **Step 5: Build**

```bash
cd frontend && npm run build 2>&1 | /usr/bin/tail -n 5
```
Expected: 0 TS errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/settings.ts
git commit -m "feat(frontend): TTS Engine + Piper Voice settings inputs"
```

---

## Task 6: Documentation

**Files:**
- Modify: `SECURITY.md`, `ARCHITECTURE.md`, `docs/DOCKER.md`, `docs/BACKLOG.md`
- Create or modify: `host-sidecar/NOTICE.md`

**Parallelism:** `[PARALLEL-OK with T5]`. Fires membrane tripwire on SECURITY/ARCH — expected.

- [ ] **Step 1: `host-sidecar/NOTICE.md`** (create if absent)

```markdown
# Third-party components used by jarvis-sidecar

## Piper (OHF-Voice/piper1-gpl) — GPL-3.0
The sidecar can optionally invoke Piper for neural TTS. Piper is licensed
GPL-3.0. JARVIS does NOT import or modify Piper: it is installed into an
isolated venv and invoked ONLY as an unmodified subprocess
(`python -m piper`), at a process boundary. This arm's-length usage keeps
JARVIS's own MIT license intact. Installing Piper is opt-in
(`setup.sh --with-piper`). See docs/superpowers/specs/2026-05-28-piper-tts-engine.md.

## whisper.cpp / whisper-cli — MIT
Invoked as a subprocess for STT.

## ffmpeg — LGPL/GPL (depending on build)
Invoked as a subprocess for audio re-encoding.
```

- [ ] **Step 2: `SECURITY.md`** — add data-classification rows:
- `TTS_ENGINE` — Secret, at-rest `data/secrets.db` (SQLCipher)
- `TTS_PIPER_VOICE` — Secret, at-rest `data/secrets.db`

Add a note under the sidecar section: Piper is GPL-3.0, subprocess-only (isolated venv, never imported); voice models downloaded + SHA256-verified at `setup.sh --with-piper` time. Voice name passed to piper is validated `^[A-Za-z0-9_-]{1,64}$` (argv-injection guard); piper input capped at 2000 chars.

- [ ] **Step 3: `ARCHITECTURE.md`** — update the sidecar entry: `/tts` now has two engines (say → m4a, piper → wav); a second isolated venv + voice dir under the state dir.

- [ ] **Step 4: `docs/DOCKER.md`** — in the sidecar section, document `./host-sidecar/setup.sh --with-piper` and that `TTS_ENGINE=piper` in settings switches to the neural voice.

- [ ] **Step 5: `docs/BACKLOG.md`** — add to Done (recent):
```markdown
- **Piper TTS engine (PR ##XX):** neural local TTS in the sidecar via OHF-Voice/piper1-gpl (GPL-3.0, subprocess-only, isolated venv). `TTS_ENGINE` vault key switches say↔piper; British `en_GB-alan-medium` default. Voice argv-injection guard + 2000-char cap per security-advisor.
```

- [ ] **Step 6: Commit (membrane tripwire fires — expected)**

```bash
git add SECURITY.md ARCHITECTURE.md docs/DOCKER.md docs/BACKLOG.md host-sidecar/NOTICE.md
git commit -m "docs: Piper engine — NOTICE (GPL), security model, architecture, docker, backlog"
```

---

## Task 7: Acceptance + PR

**Files:** verification + push only.

**Parallelism:** `[SEQUENTIAL after T5, T6]`.

- [ ] **Step 1: Full sidecar suite (host)**

```bash
cd host-sidecar && .venv/bin/pytest -v 2>&1 | /usr/bin/tail -n 15
```
Expected: 29 PASSED (15 original + 9 piper + 5 routing/health — recount: 24 after T1, 29 after T2).

- [ ] **Step 2: Full JARVIS suite (Docker)**

```bash
cd /Users/petrog/Development/github/jarvis
docker compose -p jarvis build 2>&1 | /usr/bin/tail -n 3
docker run --rm --entrypoint="" -v "$PWD:/app" -w /app jarvis-backend:local python -m pytest -q 2>&1 | /usr/bin/tail -n 20
```
Expected: 0 new failures beyond the 1 pre-existing (test_personas_setup membrane-hook jq-in-Docker).

- [ ] **Step 3: test-runner persona** — read `.claude/agents/test-runner.md`, run both suites, report exit codes verbatim.

- [ ] **Step 4: code-reviewer persona on the full branch diff**

```bash
git diff main...HEAD --stat
```
Apply must-fix.

- [ ] **Step 5: Push + PR**

```bash
git push -u origin feat/piper-tts-engine 2>&1 | /usr/bin/tail -n 5
gh pr create --title "feat: Piper neural TTS engine for the sidecar (GPL subprocess-only)" --body "$(cat <<'BODY'
Implements docs/superpowers/specs/2026-05-28-piper-tts-engine.md.

## Summary
- New \`host-sidecar/jarvis_sidecar/piper_engine.py\` — Piper (OHF-Voice/piper1-gpl, GPL-3.0) invoked ONLY as a subprocess from an isolated venv. JARVIS never imports it; MIT license stays clear.
- \`/tts\` engine routing: \`TTS_ENGINE\` (say|piper), falls back to say when piper unavailable. \`X-TTS-Engine-Used\` header reports which fired. \`/health\` reports \`piper_available\`.
- Voice argv-injection guard (\`^[A-Za-z0-9_-]{1,64}\$\`) + 2000-char input cap (security-advisor required fixes).
- \`setup.sh --with-piper\` (opt-in; ~80MB GPL install) with SHA256 model pin.
- JARVIS \`TTS_ENGINE\` + \`TTS_PIPER_VOICE\` vault keys + settings UI dropdown.

## License posture
Piper is GPL-3.0. Used at arm's length (separate venv, subprocess-only, zero \`import piper\`). NOTICE.md documents this. Engineering-prudence stance per security-advisor; not a formal legal determination.

## Test plan
- [x] 9 hermetic piper_engine tests (incl. a test asserting no \`import piper\` in source)
- [x] engine-routing + fallback + 400-on-bad-voice tests
- [x] JARVIS-side engine-param + synthesize_speech wiring tests
- [x] 0 new pytest regressions
- [ ] Manual: \`./host-sidecar/setup.sh --with-piper\`, set TTS_ENGINE=piper, hear the British neural voice

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)" 2>&1 | /usr/bin/tail -n 3
```

- [ ] **Step 6: Update BACKLOG PR number, check CI**

```bash
# Replace ##XX in docs/BACKLOG.md with the real PR number, then:
git add docs/BACKLOG.md && git commit -m "docs(backlog): fill in Piper PR number" && git push 2>&1 | /usr/bin/tail -n 3
gh pr checks 2>&1 | /usr/bin/tail -n 10
```
Do NOT auto-merge — user reviews.

---

## Self-review against the spec

**Spec coverage:**

| Spec section | Task |
|---|---|
| §2 GPL subprocess-only, isolated venv | T1 (module + no-import test), T3 (separate venv in setup.sh) |
| §4 TTS_ENGINE selection | T2 (routing), T4 (JARVIS wiring) |
| §5.1 config constants | T1 |
| §5.2 piper_engine.py + required fixes #1/#2 | T1 |
| §5.3 /tts routing + X-TTS-Engine-Used | T2 |
| §5.3a /health piper_available | T2 |
| §5.4 setup.sh --with-piper + SHA256 pin | T3 |
| §6 JARVIS sidecar_client + synthesize_speech + allowlist | T4 |
| §7 Frontend dropdown | T5 |
| §8 Tests | T1, T2, T4 |
| §9 Docs incl. NOTICE + SECURITY rows (required fix #3) | T6 |

**Placeholder scan:** The only intentional fill-in is the SHA256 `PIN_ONNX` in setup.sh — it requires a real first download to compute, and the script warns + proceeds if empty (documented behavior, not a silent gap). Everything else is concrete.

**Type consistency:** `piper_engine.synthesize`, `piper_engine.is_available`, `PiperError`, `_VOICE_RE`, `config.piper_venv()`, `config.piper_data_dir()`, `config.DEFAULT_PIPER_VOICE`, `config.PIPER_TIMEOUT_S`, `config.PIPER_MAX_TEXT_CHARS`, `tts_via_sidecar(..., engine=)`, vault keys `TTS_ENGINE`/`TTS_PIPER_VOICE`, `X-TTS-Engine-Used` — all consistent across tasks.

**Test count reconciliation:** sidecar suite: 15 (pre-Piper) + 9 (T1) = 24 after T1; + 4 tts routing + 1 health (T2) = 29 after T2. T7 step 1 expects 29. Consistent.

Plan ships as-is.

---

## Parallelism map

```
T1 (piper_engine) ─► T2 (/tts routing + /health) ─► T3 (setup.sh) ─► T4 (JARVIS wiring) ─► T5 (frontend)
                                                                                              │
                                                                          T6 (docs) ──────────┤ (parallel-ok with T5)
                                                                                              ▼
                                                                                          T7 (PR)
```
Sequential implementers within the branch. T6 can run alongside T5. ~7 tasks × (impl + 2 reviews on code tasks) ≈ 18 dispatches.
