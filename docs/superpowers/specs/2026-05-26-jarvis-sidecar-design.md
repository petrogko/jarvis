# JARVIS Host-Sidecar — Combined TTS + STT Design

**Status:** design (for review)
**Date:** 2026-05-26
**Backlog items:** **merges P3a (Whisper STT) + P13 (TTS host-sidecar)** into a single deliverable.
**Persona routing:** `software-architect` validated this scope via brainstorming → `security-advisor` MUST review before implementation (new trust boundary: host↔container HTTP, new long-running service, new shared-secret file).

---

## 1. Goals

1. **Eliminate the two remaining third-party voice egresses.** Today, Chrome Web Speech ships user-recorded audio to Google's servers (STT). Fish Audio receives JARVIS's response text (TTS) when running in Docker. Both end here.
2. **Preserve Docker isolation.** JARVIS keeps running in its hardened container. The sidecar is a separate macOS process the container talks to over loopback.
3. **One service, two endpoints.** A single `jarvis-sidecar` daemon exposes `/tts` and `/stt`. Single install, single launchctl plist, single token, single port. Per the brainstorming decision.
4. **Use battle-tested local engines.** macOS `say` for TTS; `whisper.cpp` for STT (Metal-accelerated on M-series). No new cloud dependencies.
5. **Chunked transcription, not streaming.** v1 records audio then transcribes (walkie-talkie UX). Streaming Whisper is out of scope.

## 2. Non-goals

- Streaming partial transcripts during speech (research territory; v1 = chunked).
- Multiple Whisper language models or sizes beyond `base.en` (English-only, ~150 MB; future PR can add `small`, `medium`, multilingual).
- Speaker diarization or speaker IDs.
- GPU/Metal flag tuning beyond whisper.cpp defaults.
- Auto-update of the sidecar binary or model.
- LAN reachability — sidecar is `127.0.0.1`-only.
- Running the sidecar in Linux/Docker. macOS host only.
- Replacing the existing `openclaw_ports/tts_local_cli` (PR #15) — that module remains the on-host path; the sidecar absorbs the TTS surface for the Docker deployment.

## 3. Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│ macOS host                                                          │
│                                                                     │
│  ┌──────────────────────────────┐    ┌────────────────────────────┐ │
│  │ Browser (localhost:5173)     │    │ jarvis-sidecar (127.0.0.1: │ │
│  │  - MediaRecorder → audio     │    │   9999)                    │ │
│  │  - audioPlayer / speak       │    │                            │ │
│  └──────────────┬───────────────┘    │   POST /tts ──► `say` ──►  │ │
│                 │ /api/stt           │   POST /stt ──► whisper.   │ │
│                 │ {type:audio,...}   │             cpp main       │ │
│                 ▼                    │   GET  /health             │ │
│  ┌──────────────────────────────┐    │                            │ │
│  │ Docker: jarvis-backend       │    │  Reads token from          │ │
│  │  ┌───────────────────────┐   │    │  ~/Library/Application     │ │
│  │  │ FastAPI server.py     │   │    │  Support/jarvis-sidecar/   │ │
│  │  │   /api/stt POST       │───┼────┼──► token (chmod 600)       │ │
│  │  │   synthesize_speech() │   │    │                            │ │
│  │  └───────────────────────┘   │    │  Binds 127.0.0.1 only      │ │
│  │  Calls host.docker.internal  │    │                            │ │
│  │       :9999 with X-JARVIS-   │    │  launchctl plist at        │ │
│  │       Token header           │    │  ~/Library/LaunchAgents    │ │
│  └──────────────────────────────┘    └────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────┘
```

### 3.1 Sidecar service

- **Repo location:** `host-sidecar/` at repo root. Self-contained Python package.
- **Language:** Python 3.11 (matches JARVIS).
- **Framework:** FastAPI + uvicorn (familiar pattern; ~80 LOC for the app).
- **Bind:** `127.0.0.1:9999` only — NEVER `0.0.0.0`. Hardcoded.
- **Process model:** Single uvicorn worker. STT requests are CPU-bound on whisper.cpp; concurrent calls serialize at the subprocess level (no need for worker pool in v1).
- **Distribution:** `host-sidecar/setup.sh` script:
  1. `brew install whisper-cpp ffmpeg`
  2. Download `base.en` GGML model to `~/Library/Application Support/jarvis-sidecar/models/`
  3. Generate auth token (32 random bytes, base64); write to `~/Library/Application Support/jarvis-sidecar/token` (chmod 600)
  4. Install `~/Library/LaunchAgents/com.jarvis.sidecar.plist`; `launchctl load` it
- **Logs:** stderr → `~/Library/Logs/jarvis-sidecar.log`. Rotation: 5 files × 5 MB via Python `RotatingFileHandler`.

### 3.2 Endpoints

| Endpoint | Method | Body | Response | Auth |
|---|---|---|---|---|
| `/health` | GET | — | `{"status":"ok","whisper_model":"base.en","say_available":true}` | None (loopback only; allows ops checks without token) |
| `/tts` | POST | `{"text": str, "voice": str}` (JSON) | `audio/m4a` bytes | `X-JARVIS-Token` header required |
| `/stt` | POST | `multipart/form-data` — `audio` field with WebM/Opus or WAV | `{"text": str, "duration_ms": int}` | `X-JARVIS-Token` header required |

`/tts` implementation: same shape as `openclaw_ports/tts_local_cli` — argv-only `say` invocation with M4A/AAC output. ~30 LOC.

`/stt` implementation:
1. Save uploaded audio to temp file
2. `ffmpeg -i <upload> -ar 16000 -ac 1 -f wav <pcm.wav>` (whisper.cpp wants 16kHz mono WAV)
3. `whisper-cli -m models/base.en.bin -f pcm.wav -nt -np -otxt -of <out>` (no-timestamps, no-progress, output text)
4. Read `<out>.txt`, return trimmed string
5. Always clean up temp files in `finally`

Timeout per request: 60s (`asyncio.wait_for` around the subprocess). Audio uploads capped at 5 MB (FastAPI dependency).

### 3.3 Auth model

- **Single shared token** between JARVIS and sidecar, stored at `~/Library/Application Support/jarvis-sidecar/token` (chmod 600).
- Sidecar reads it on boot; JARVIS reads it from the SAME file path via a host bind-mount in `docker-compose.yml` (mount the directory read-only into `/host-sidecar-config/`).
- JARVIS sends every request with `X-JARVIS-Token: <token>` header. Sidecar rejects with 401 otherwise.
- The token is **independent** of the vault's `AUTH_TOKEN`. Two separate trust contexts (browser↔JARVIS vs JARVIS↔sidecar) get two separate tokens. Simpler to reason about.

## 4. JARVIS backend changes

### 4.1 New module: `sidecar_client.py`

```python
async def tts_via_sidecar(text: str, voice: str = "Alex") -> bytes | None
async def stt_via_sidecar(audio_bytes: bytes, mime_type: str = "audio/webm") -> str
async def sidecar_health() -> dict | None
```

Reads sidecar URL from vault key `SIDECAR_URL` (default `http://host.docker.internal:9999`). Reads sidecar token from `/host-sidecar-config/token` (the bind-mount). All three functions are best-effort: on connection error, `None` (TTS) or `""` (STT) or `None` (health).

### 4.2 `synthesize_speech` dispatcher (extends T7 from gh-issues PR)

New `TTS_PROVIDER` value: `sidecar`. Provider table:

| `TTS_PROVIDER` | Behavior |
|---|---|
| `auto` (default) | Try local `say` (host-only) → sidecar (Docker) → Fish Audio (cloud fallback) |
| `local_cli` | macOS `say` only; None if unavailable |
| `sidecar` | Sidecar only; None if unreachable |
| `fish_audio` | Fish only |

### 4.3 New endpoint `POST /api/stt`

Body: `multipart/form-data` with `audio` field. Returns `{"text": str}`. Implementation: pass through to `sidecar_client.stt_via_sidecar`. Reuses the existing auth middleware.

## 5. Frontend changes

### 5.1 New module: `frontend/src/stt.ts`

```ts
export interface RecordingSession {
  stop(): Promise<string>;  // resolves with transcript text
  cancel(): void;
}
export function startRecording(): RecordingSession
```

Implementation: `MediaRecorder` capturing `audio/webm;codecs=opus` (Chrome/Safari default). On `stop()`, POSTs the recorded blob to `/api/stt`, returns transcript text. On `cancel()`, abort and discard.

### 5.2 `voice.ts` + `main.ts` changes

- New vault key `STT_PROVIDER` ∈ {`web_speech`, `whisper`}. Default `web_speech` (current behavior).
- When `STT_PROVIDER === "whisper"`, the mic button becomes a **record toggle** (instead of always-listening). First click: start recording, orb pulses. Second click (or stop button): stop recording, await transcription, send the resulting transcript through the existing `socket.send({type:"transcript", text, isFinal:true})` path. Identical downstream behavior.

### 5.3 Settings UI

New section "STT Provider" with a dropdown: `Web Speech (browser/Google)` | `Whisper (local sidecar)`. Save handler updates vault key.

## 6. Docker compose changes

Add a single bind mount:

```yaml
services:
  backend:
    volumes:
      - ./data:/app/data:rw
      # NEW: read-only access to the sidecar token. The sidecar owns
      # this dir on the host; JARVIS reads the token.
      - ~/Library/Application Support/jarvis-sidecar/:/host-sidecar-config/:ro
    # NEW: ensure host.docker.internal resolves (Docker Desktop does this
    # automatically on macOS but adding extra_hosts is explicit and safe).
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

No new environment variables; the sidecar URL is in the vault as `SIDECAR_URL`.

## 7. Trust boundary

| Boundary | What crosses it | Defense |
|---|---|---|
| Browser ↔ JARVIS Docker | User audio (POST `/api/stt`), JARVIS response audio (WS `audio` message) | Existing JARVIS vault token (`X-JARVIS-Token`), TLS in production |
| JARVIS Docker ↔ host sidecar | Audio bytes (in), text (out); response text (in), audio bytes (out) | Loopback bind on sidecar; second token in the `X-JARVIS-Token` header; bind mount is RO so JARVIS can't tamper with the token file |
| Sidecar ↔ `say` / `whisper.cpp` | argv-passed text/audio file path | argv-only invocation, no shell, no f-string interpolation. Same hardening as `openclaw_ports/tts_local_cli` |
| Sidecar ↔ disk | Temp files (audio uploads, whisper output) | Files created in `tempfile.mkdtemp(prefix="jarvis-sidecar-")`, deleted in `finally`. Never reused across requests. |

### 7.1 Threat model deltas

- **New attack surface:** loopback HTTP server on port 9999.
- **Mitigation:** binds 127.0.0.1 only; rejects without correct `X-JARVIS-Token`; sidecar logs only metadata (request count, duration, bytes — never text or audio content).
- **Existing JARVIS hardening invariants preserved:** vault, auth middleware, untrusted-content sanitizer, audit log all unchanged.
- **NOT defended against:** another process on the same Mac that can read the token file (chmod 600 + macOS user separation is the only defense). The user's own account is the trust boundary; same as `data/secrets.db` for the vault.

## 8. Tests

### 8.1 Hermetic (run in CI)

- `host-sidecar/tests/test_endpoints.py` — uses FastAPI TestClient + mocked subprocess. Tests:
  - `/health` returns 200 with the expected shape
  - `/tts` returns 200 + bytes when `say` succeeds; 503 when not on macOS (mocked)
  - `/stt` returns 200 + text when whisper.cpp succeeds; 500 + JSON error when it fails
  - `X-JARVIS-Token` rejected when missing or wrong
  - Multipart body size cap rejects >5 MB with 413
- `tests/test_sidecar_client.py` — mocks `httpx` to simulate sidecar responses. Tests:
  - `tts_via_sidecar` returns bytes on 200, None on connection error
  - `stt_via_sidecar` returns text on 200, "" on connection error
  - `sidecar_health` returns dict on 200, None on connection error
  - All three send the X-JARVIS-Token header

### 8.2 Manual integration (per the existing `tests/test_classifier.py` / `tests/test_goal_drift.py` excluded-by-default pattern)

- Live `host-sidecar/tests/integration/test_live.py` (excluded from default pytest collection). Requires the sidecar running + a real audio file. Confirms end-to-end TTS roundtrip + STT on a known WAV ("hello world" → expected text contains "hello").

## 9. Documentation updates required

- `SECURITY.md`: new trust-boundary row (Docker ↔ sidecar). New data-classification entry for the sidecar token. Note that voice audio + transcript text are now never sent over the public internet when STT_PROVIDER=whisper + TTS_PROVIDER ∈ {auto, local_cli, sidecar}.
- `ARCHITECTURE.md`: add `host-sidecar/` to the module map. Update the voice-loop diagram (text → synthesize_speech → sidecar OR Fish; audio → /api/stt → sidecar).
- `docs/DOCKER.md`: new section "Optional host sidecar for local TTS/STT" with the setup steps + the egress allowlist (sidecar means `host.docker.internal` joins the allowed-egress list when `TTS_PROVIDER=sidecar` or `STT_PROVIDER=whisper`).
- `CLAUDE.md`: persona routing — edits to `host-sidecar/` invoke `security-advisor` (new daemon surface).
- `docs/BACKLOG.md`: mark P3a and P13 as merged-then-done after the implementation PR lands.

## 10. Implementation order (per writing-plans)

1. **Sidecar service** — `host-sidecar/` package, `/health` + `/tts` + `/stt` endpoints, token auth, setup.sh. Hermetic tests using TestClient + mocked subprocess. ~400 LOC across app + tests.
2. **JARVIS backend wiring** — `sidecar_client.py`, `synthesize_speech` extension, `/api/stt` endpoint. ~150 LOC.
3. **Frontend STT path** — `stt.ts` module, voice.ts mode switch, settings UI dropdown. ~120 LOC.
4. **Docker compose changes** — bind mount, `extra_hosts`. ~5 LOC.
5. **Documentation + acceptance** — SECURITY.md, ARCHITECTURE.md, DOCKER.md, CLAUDE.md, BACKLOG.md updates; full acceptance run.

Total estimated effort: ~700 LOC + tests + docs. Implementable in 6–10 sub-tasks via `superpowers:subagent-driven-development`.

## 11. Open questions for security-advisor

1. **Token file as the JARVIS↔sidecar trust root** — is mounting `~/Library/Application Support/jarvis-sidecar/` read-only into the container the right shape? Alternative: pass the token via Docker secret or env var (worse — env vars are visible to anything that can read /proc on the host).
2. **Logging discipline** — the sidecar logs request metadata only (duration, bytes). Confirm `whisper.cpp`'s stderr (which may contain progress info or audio-derived metadata) is captured to the rotating log file and not stdout/stderr that could leak elsewhere.
3. **Process lifetime** — should the sidecar exit if its config dir disappears? If JARVIS is uninstalled but the launchctl plist stays loaded, the sidecar runs orphan. Document the uninstall flow.
4. **Audio temp files** — `tempfile.mkdtemp` creates files in `/tmp` by default (which is `/var/folders/...` on macOS, user-owned). Acceptable per the threat model — same place macOS `say` writes its output. Confirm.
5. **Resource limits** — should we cap memory/CPU on the sidecar via `launchctl` limits? Whisper base.en uses ~600 MB RSS; a malformed audio file could push it higher.

## 12. Out-of-scope follow-ups (will be separate PRs)

- Streaming Whisper (partial transcripts during speech)
- Multi-language Whisper models
- Whisper model auto-update / version pinning
- A "JARVIS uninstaller" script that unloads the launchctl plist and removes the token file
- Sidecar process recovery (auto-restart on crash via `KeepAlive` in plist — should be default, document)
- Larger-than-5MB audio support (chunked upload?)
