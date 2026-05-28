# Piper TTS Engine for the Sidecar — Design

**Status:** design (for review)
**Date:** 2026-05-28
**Extends:** `docs/superpowers/specs/2026-05-26-jarvis-sidecar-design.md` (the host sidecar). This adds a second TTS engine alongside macOS `say`.
**Persona routing:** edits to `host-sidecar/` → `security-advisor` (per CLAUDE.md) — focus on the GPL arm's-length boundary + voice-model download integrity. Then `code-reviewer` per commit, `test-runner` before "ready".

---

## 1. Goal

Give JARVIS access to natural neural voices (incl. British English) via **Piper**, while keeping JARVIS MIT-licensed. The sidecar's `/tts` endpoint gains a second engine; the macOS `say` path stays as the default/fallback.

## 2. The licensing constraint (load-bearing)

- The maintained Piper is **`OHF-Voice/piper1-gpl` — GPL-3.0** (the MIT `rhasspy/piper` was archived Oct 2025).
- **JARVIS must NEVER `import piper`.** That would make JARVIS a GPL derivative work.
- Piper is invoked **only as a subprocess** (`<piper-venv>/bin/python -m piper ...`), at a process boundary — the same arm's-length stance used for `say`, `whisper-cli`, `ffmpeg`, and (in spirit) the WorldMonitor AGPL decision.
- Piper is installed into its **own isolated venv** (`~/Library/Application Support/jarvis-sidecar/piper-venv/`), separate from the sidecar's FastAPI venv, so there is zero chance the sidecar code imports it. The sidecar execs the piper venv's python.
- `NOTICE.md` + `SECURITY.md` must document: Piper is GPL-3.0, used as an unmodified subprocess binary; JARVIS does not link or import it.

## 3. Non-goals

- Voice cloning (that's XTTS/F5-TTS territory — separate future spec).
- Bundling Piper voices in the repo (they're downloaded at setup time).
- Replacing `say` — `say` remains the default engine and the fallback when Piper is unavailable.
- Multiple simultaneous Piper voices / runtime voice switching beyond a single configured default (can extend later).
- Streaming synthesis.

## 4. Engine selection

New vault key **`TTS_ENGINE`** ∈ {`say`, `piper`}, default `say`.

Flow: JARVIS's `sidecar_client.tts_via_sidecar` reads `TTS_ENGINE` from the vault and includes it in the `/tts` POST body. The sidecar routes on it. If `piper` is requested but unavailable (binary or model missing), the sidecar falls back to `say` and notes it in the response header `X-TTS-Engine-Used`.

So the full TTS decision tree is now:
1. `TTS_PROVIDER` (vault) decides local-say / sidecar / fish (existing, JARVIS-side).
2. When the sidecar is chosen, `TTS_ENGINE` (vault, passed in the POST body) decides `say` vs `piper` (new, sidecar-side).

## 5. Sidecar changes

### 5.1 `host-sidecar/jarvis_sidecar/config.py`
Add:
```python
PIPER_VENV: Final = state_dir() / "piper-venv"          # isolated; never imported
PIPER_DATA_DIR: Final = state_dir() / "piper-voices"    # downloaded .onnx + .onnx.json
DEFAULT_PIPER_VOICE: Final[str] = "en_GB-alan-medium"   # British English; configurable
PIPER_TIMEOUT_S: Final[float] = 30.0
```

### 5.2 New module `host-sidecar/jarvis_sidecar/piper_engine.py`
Named `piper_engine.py` (NOT `piper.py`) to eliminate any `import piper`
shadowing landmine — the GPL boundary depends on never importing piper.
```python
class PiperError(RuntimeError): ...

def is_available() -> bool:
    """True iff the piper venv python + the default voice .onnx both exist."""

async def synthesize(text: str, voice: str) -> bytes:
    """Run `<PIPER_VENV>/bin/python -m piper -m <voice> -f <out.wav> -- <text>`
    in PIPER_DATA_DIR (so it finds the model). Returns WAV bytes.

    Subprocess-only (GPL arm's length). DEVNULL on stdout/stderr. Temp file
    cleaned in finally. Empty text → PiperError. Timeout enforced."""
```

### 5.3 `/tts` route (`app.py`)
Extend `_TTSBody` with `engine: str = "say"`. Route:
```python
if body.engine == "piper" and piper.is_available():
    audio = await piper.synthesize(body.text, _piper_voice())
    return Response(audio, media_type="audio/wav", headers={"X-TTS-Engine-Used": "piper"})
# else fall through to say (existing path), header X-TTS-Engine-Used: say
```
Output format differs by engine: `say` → `audio/m4a` (AAC), `piper` → `audio/wav`. Both decode fine in the browser's Web Audio API. The `X-TTS-Engine-Used` header tells the caller which fired.

### 5.4 `setup.sh`
Add steps:
- Create `PIPER_VENV` (separate from the sidecar venv).
- `<PIPER_VENV>/bin/pip install piper-tts`.
- `<PIPER_VENV>/bin/python -m piper.download_voices en_GB-alan-medium --data-dir <PIPER_DATA_DIR>`.
- Idempotent (skip if voice .onnx already present).
- Make the Piper steps OPTIONAL with a `--with-piper` flag, OR prompt — Piper + model is ~60-80 MB and GPL; users who only want `say` shouldn't be forced to install it. **Decision:** gate behind `setup.sh --with-piper`; default setup installs whisper + say only.

### 5.5 `teardown.sh`
Remove `PIPER_VENV` + `PIPER_DATA_DIR` along with the rest of the state dir (already `rm -rf`'d — no change needed, but verify).

## 6. JARVIS backend changes

- `sidecar_client.tts_via_sidecar(text, voice, engine="say")` — add `engine` param, include in POST body.
- `synthesize_speech`: when calling the sidecar path, read `TTS_ENGINE` from vault and pass it.
- Vault allowlist: add `TTS_ENGINE`, `TTS_PIPER_VOICE` (so the British voice is user-overridable).
- `/api/settings/preferences`: expose `tts_engine`.

## 7. Frontend

- Settings UI: a "TTS Engine" dropdown (`System (say)` | `Piper (neural)`) saved to `TTS_ENGINE`.
- Optional: a "Piper Voice" text input → `TTS_PIPER_VOICE` (default `en_GB-alan-medium`).

## 8. Tests

Hermetic (mock subprocess), under `host-sidecar/tests/test_piper.py`:
- `is_available` true/false matrix (venv python present, model present)
- `synthesize` happy path — asserts argv is `[<piper-venv>/bin/python, -m, piper, -m, <voice>, -f, <out>, --, <text>]`, DEVNULL on both streams, returns WAV bytes, temp file cleaned
- empty text → PiperError
- nonzero exit → PiperError
- `/tts` with `engine=piper` + piper unavailable → falls back to say, `X-TTS-Engine-Used: say`
- `/tts` with `engine=piper` + available → `X-TTS-Engine-Used: piper`, `audio/wav`

JARVIS-side: `test_sidecar_client.py` — `tts_via_sidecar(engine="piper")` includes `engine` in the POST body.

## 9. Documentation

- `host-sidecar/NOTICE.md` (or a new one if absent): Piper is GPL-3.0, invoked as an unmodified subprocess from an isolated venv; JARVIS does not import or modify it.
- `SECURITY.md`: note the GPL-subprocess boundary; voice models downloaded from the Piper project's hosting at setup time (integrity: see open question §11).
- `ARCHITECTURE.md`: sidecar now has two TTS engines.
- `docs/DOCKER.md`: `setup.sh --with-piper` documented.
- `docs/BACKLOG.md`: mark this done.

## 10. Implementation order

1. Sidecar `config.py` + `piper.py` module + hermetic tests
2. `/tts` engine routing + `X-TTS-Engine-Used` header + tests
3. `setup.sh --with-piper` + teardown verify
4. JARVIS `sidecar_client` engine param + `synthesize_speech` wiring + vault allowlist + tests
5. Frontend TTS Engine dropdown
6. Docs (NOTICE/SECURITY/ARCHITECTURE/DOCKER/BACKLOG)
7. Acceptance + PR

## 11. Open questions for security-advisor

1. **Voice-model download integrity.** `piper.download_voices` fetches `.onnx` weights over the network at setup time. Is there a checksum/signature? If not, a MITM could swap the model. Mitigation options: pin a known SHA256 in setup.sh and verify after download; or document the risk. What's the right bar for a single-user local tool?
2. **GPL arm's-length confirmation.** Confirm that (a) installing piper-tts in an isolated venv and (b) invoking via `subprocess.exec([piper_venv_python, "-m", "piper", ...])` with NO `import piper` anywhere in JARVIS/sidecar code keeps JARVIS clear of GPL-3.0 copyleft. Flag if the `python -m piper` form (vs a standalone binary) weakens the boundary.
3. **Module naming — RESOLVED.** Module is `piper_engine.py`, not `piper.py`, to eliminate any `import piper` shadowing risk. (Was an open question; resolved in §5.2.)
4. **WAV size / DoS.** Piper WAV output for a long response could be large. Should the sidecar cap synthesis input length? `say` has no such cap today. Consistency vs. safety.
5. **Subprocess resource use.** Piper loads an ONNX model per invocation (cold start ~1-2s). Acceptable for v1, or should the sidecar keep a warm piper process? (Leaning: cold start is fine for v1; note as future optimization.)

## 12. Out-of-scope follow-ups
- Warm/persistent piper process for lower latency
- Multiple downloadable voices + runtime switching UI
- Voice cloning (XTTS/F5-TTS)
- GPU acceleration flags
