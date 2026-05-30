# Sidecar `/spawn` — Claude Code from Docker

**Status:** design (for security-advisor review)
**Date:** 2026-05-29
**Extends:** `docs/superpowers/specs/2026-05-26-jarvis-sidecar-design.md`
**Persona routing:** new daemon surface — `security-advisor` first (CLAUDE.md routing for `host-sidecar/`).

---

## 1. Goal

Make `[ACTION:BUILD]`, `[ACTION:RESEARCH]`, and `[ACTION:PROMPT_PROJECT]` work when JARVIS runs in Docker. Today the dispatch path calls `claude_runner` which spawns `claude -p --dangerously-skip-permissions` directly; the container has no `claude` CLI and no host shell, so the spawn silently fails and Aria's most powerful actions are dead weight.

Solution: a new `/spawn` endpoint on the existing host-sidecar daemon (which already runs on the macOS host). JARVIS POSTs `{prompt, workdir}`, the sidecar runs `claude` on the host, JARVIS polls for status and final output. Same arm's-length subprocess boundary as `/tts` (say) and `/stt` (whisper-cli).

## 2. Threat model

- Caller: JARVIS-in-Docker, authenticated via shared `X-SIDECAR-Token` header (existing posture).
- Sidecar binds `127.0.0.1:9999` only — no LAN exposure.
- Single-user Mac. FileVault is the at-rest defense. "Attacker with shell on host" is explicitly NOT in-scope (already documented in `SECURITY.md`).
- New risk surface: the endpoint runs `claude -p --dangerously-skip-permissions` (or `--permission-mode bypassPermissions` — equivalent), which means **the prompt + the workdir are the sole guards** on what claude does on disk. Both flow from JARVIS, which sources them from LLM-classified intent. A prompt-injection at the LLM layer that reaches this endpoint can do whatever the macOS user can do under the constrained `cwd`. This is the same threat JARVIS-on-host already lives with (`claude_runner.py` + `cwd_allowlist.py`); we are not increasing it, only relocating where the spawn happens.

## 3. Non-goals

- Streaming output (SSE/WebSocket). Polling-based status only — adequate for current JARVIS usage; streaming can be added as a follow-up.
- Multi-agent support (`codex`, `opencode`, `pi`). Defer to a follow-up spec; first wave is claude-only.
- Persistent session state across sidecar restarts. In-memory registry only.
- Returning intermediate stdout chunks. The final captured stdout/stderr is returned on completion.
- Containerized claude (the `dcdda7c`/`7fffaca` ephemeral-Docker path). That's an orthogonal hardening; this endpoint always runs claude directly on the host so we have one clear pattern.

## 4. API

### `POST /spawn`
Request body:
```json
{
  "prompt": "string (≤ PROMPT_MAX_CHARS)",
  "workdir": "absolute path on host (must pass allowlist)",
  "agent": "claude",
  "timeout_s": 600
}
```
Response 200:
```json
{"session_id": "uuid4-string", "status": "running", "started_at": 1717000000.0}
```
Errors:
- 400 — empty prompt, prompt too large, missing/invalid workdir, workdir not in allowlist, unknown agent, timeout out of range
- 401 — bad/missing `X-SIDECAR-Token`
- 429 — too many concurrent sessions (configurable cap, default 3)
- 500 — failed to spawn

### `GET /spawn/{session_id}`
Response 200:
```json
{
  "session_id": "...",
  "status": "running" | "finished" | "failed" | "timeout" | "killed",
  "exit_code": 0,
  "output": "string (≤ OUTPUT_MAX_BYTES; merged stdout+stderr)",
  "output_truncated": false,
  "started_at": 1717000000.0,
  "finished_at": 1717000001.5
}
```
Errors:
- 401 — bad/missing token
- 404 — unknown session id

### `DELETE /spawn/{session_id}`
Kill the running process (SIGKILL). Returns 200 with the same status object. Idempotent — already-finished sessions return their final state.

## 5. Security guards (load-bearing)

1. **Workdir allowlist** (mirror `cwd_allowlist.py`):
   - Roots = `~/Desktop`, `JARVIS_EXTRA_PROJECT_DIRS` (comma-separated env var), and `~/Development` (configurable). The JARVIS repo root is NOT in the sidecar's allowlist — sidecar must never spawn claude inside itself or inside JARVIS's own repo.
   - Resolve via `Path(p).expanduser().resolve(strict=False)` BEFORE matching, so `..` and symlinks are flattened. (Existing `cwd_allowlist.py` pattern.)
   - Workdir must EXIST as a directory. No auto-create on the sidecar side — JARVIS creates project dirs through other means.
   - Rejection → 400, audit log entry written.
2. **Prompt size cap** — `PROMPT_MAX_BYTES = 64 * 1024` (64 KiB). Larger → 400.
3. **Prompt via stdin pipe** — `asyncio.create_subprocess_exec` with `stdin=PIPE`, `await proc.communicate(input=prompt_bytes)`. No shell, no temp file, no f-string interpolation. (`claude_runner.py` precedent: "no leakage to `ps`, no length limit".)
4. **Argv allowlist** — exact list, no user-controlled flags:
   ```python
   argv = ["claude", "-p", "--output-format", "text", "--dangerously-skip-permissions"]
   ```
5. **Agent allowlist** — only the literal string `"claude"` accepted in the request body. Anything else → 400. Reserved for future expansion.
6. **Timeout** — `timeout_s` clamped to [60, 1800]. Default 600. Process gets SIGTERM then SIGKILL on expiry. Status → `timeout`.
7. **Concurrency cap** — `SPAWN_MAX_CONCURRENT = 3`. 4th simultaneous spawn → 429.
8. **Output cap** — `OUTPUT_MAX_BYTES = 1 * 1024 * 1024` (1 MiB). Output beyond is dropped; response carries `output_truncated: true`.
9. **DEVNULL discipline** does NOT apply here (we want output) — but the output buffer is bounded (#8) and the only consumer is JARVIS over the authed loopback. Prompt content NEVER appears in any sidecar log line.
10. **Audit log entry per spawn** — appended to `~/Library/Logs/jarvis-sidecar.log` as a single structured line: timestamp, workdir, prompt-bytes, exit-code, status, duration-ms. The prompt content is NOT logged. (Matches `audit_log` discipline from JARVIS-side `/api/stt`.)
11. **Token auth** — same `require_token` dependency that `/tts` and `/stt` use. Constant-time compare via `auth.header_matches`.
12. **TTL eviction** — finished sessions stay in the registry for `SESSION_TTL_S = 300` (5 minutes) for status polling. After TTL, they're evicted and `GET /spawn/{id}` returns 404.
13. **Resource limits** — sidecar process already constrained by macOS user limits; no rlimit/cgroup change in this PR.

## 6. Sidecar implementation

### 6.1 New module `host-sidecar/jarvis_sidecar/spawn.py`
```python
class SpawnError(RuntimeError): ...

@dataclass
class SpawnSession:
    session_id: str
    workdir: str
    prompt_bytes: int
    proc: asyncio.subprocess.Process | None
    started_at: float
    finished_at: float | None
    exit_code: int | None
    status: str   # running | finished | failed | timeout | killed
    output: bytearray
    output_truncated: bool

class SpawnManager:
    def __init__(self): ...
    async def spawn(self, prompt: str, workdir: str, timeout_s: float) -> SpawnSession: ...
    def get(self, session_id: str) -> SpawnSession | None: ...
    async def kill(self, session_id: str) -> SpawnSession | None: ...
    def _evict_expired(self): ...
```

### 6.2 New module `host-sidecar/jarvis_sidecar/cwd_allowlist.py`
Ported pattern of JARVIS's `cwd_allowlist.py` — independent copy so the sidecar has no python-path dependency on JARVIS. Reads roots from env at call time (testable). The two copies diverge only in roots (sidecar excludes the JARVIS repo).

### 6.3 `host-sidecar/jarvis_sidecar/config.py` additions
```python
PROMPT_MAX_BYTES: Final[int] = 64 * 1024
OUTPUT_MAX_BYTES: Final[int] = 1 * 1024 * 1024
SPAWN_MAX_CONCURRENT: Final[int] = 3
SPAWN_DEFAULT_TIMEOUT_S: Final[float] = 600.0
SPAWN_MIN_TIMEOUT_S: Final[float] = 60.0
SPAWN_MAX_TIMEOUT_S: Final[float] = 1800.0
SESSION_TTL_S: Final[float] = 300.0
```

### 6.4 `host-sidecar/jarvis_sidecar/app.py` — new endpoints behind the existing `require_token` dependency. Module-level `_spawn_manager = SpawnManager()` created in `create_app()`.

### 6.5 `/health` extension — add `spawn_ready: bool` (true iff `which claude` resolves on PATH).

## 7. JARVIS-side wiring

- `sidecar_client.spawn_via_sidecar(prompt, workdir, timeout_s=600) -> dict | None` and `sidecar_client.spawn_get(session_id) -> dict | None`.
- New `claude_runner` backend: `BACKEND="sidecar"`. When set, `claude_runner.run(prompt, cwd, ...)` posts to `/spawn`, polls `GET /spawn/{id}` every 2s, returns the merged output when status is terminal. Existing direct + docker backends untouched.
- Detect at startup: if running in Docker AND sidecar is reachable, default `BACKEND="sidecar"`. Otherwise keep current default. Env override: `JARVIS_CLAUDE_BACKEND=sidecar|direct|docker`.

## 8. Tests

Hermetic (mock `asyncio.create_subprocess_exec` like existing `/tts` `/stt` tests):
- `/spawn` happy path: argv is exactly `["claude","-p","--output-format","text","--dangerously-skip-permissions"]`, prompt is sent via stdin (not in argv), `cwd` is the requested workdir, status flows running → finished, GET returns merged output.
- Workdir-allowlist rejection: `/etc`, `~/Desktop/../../etc`, a workdir-under-the-JARVIS-repo path → 400 with no spawn attempt.
- Workdir does-not-exist → 400.
- Prompt too large (> 64 KiB) → 400.
- Unknown agent → 400.
- Timeout out of range → 400.
- Concurrency cap → 4th spawn returns 429.
- Output truncation — fake subprocess emits > 1 MiB; final response has `output_truncated: true` and output is exactly `OUTPUT_MAX_BYTES`.
- Hard timeout — fake proc that never exits is killed at `timeout_s`; status `timeout`.
- DELETE kills a running session and returns final state.
- GET unknown session → 404. GET expired session → 404 (after TTL).
- /health reports `spawn_ready` based on `shutil.which("claude")`.
- Source-scan test: `spawn.py` MUST NOT use `shell=True` or `os.system`; argv list is constructed without f-string interpolation.

JARVIS-side: `test_sidecar_client.py` — `spawn_via_sidecar` POSTs the right body shape, returns dict on 200, None on errors.

## 9. Documentation

- `SECURITY.md`: new "Sidecar /spawn" subsection documenting the workdir allowlist (canonical list), prompt/output caps, token auth, the bypassPermissions implication, and the "single-user Mac, host-shell-attacker out-of-scope" framing.
- `ARCHITECTURE.md`: sidecar gains a third endpoint (`/spawn`) alongside `/tts` and `/stt`.
- `docs/DOCKER.md`: document that with the sidecar installed, JARVIS-in-Docker now dispatches `claude` via the sidecar; without it, dispatches fail-soft.
- `host-sidecar/NOTICE.md`: no change (we don't depend on a new third-party package).

## 10. Implementation order

1. `cwd_allowlist.py` (sidecar) + tests.
2. `spawn.py` (SpawnManager) + hermetic tests.
3. `/spawn` POST/GET/DELETE + `/health` `spawn_ready` + tests.
4. JARVIS `sidecar_client.spawn_via_sidecar` + tests.
5. `claude_runner` "sidecar" backend wiring + tests.
6. Docs (`SECURITY.md`, `ARCHITECTURE.md`, `docs/DOCKER.md`).
7. Acceptance + PR.

## 11. Open questions (security-advisor to weigh in)

1. **bypassPermissions vs --dangerously-skip-permissions** — same effect; should the spec use the newer flag? Recommended: keep `--dangerously-skip-permissions` to mirror existing `claude_runner.py` exactly.
2. **Workdir allowlist roots** — should `~/Development` be included by default, or strictly opt-in via `JARVIS_EXTRA_PROJECT_DIRS`? Recommend opt-in only; default = `~/Desktop`.
3. **Audit log destination** — same `~/Library/Logs/jarvis-sidecar.log` as everything else, or a dedicated `spawn-audit.jsonl`?
4. **Should prompt size cap be lower** (e.g. 16 KiB)? 64 KiB matches the practical upper bound for claude-prompt-via-stdin scenarios we've observed; advisor may push back.
5. **Per-token rate limit** — current concurrency cap is global. Should there be a per-token rate limit (e.g. max 10 spawns/min)?
6. **/spawn output redaction** — output may contain secrets that claude printed back (e.g. .env contents claude was asked to inspect). Should we redact common secret patterns before returning? Recommend NO — the output goes back to JARVIS-on-loopback, same trust boundary as today's `claude_runner.stdout`.

## 12. Out-of-scope follow-ups

- SSE/WebSocket streaming.
- Multi-agent (`codex`, `opencode`, `pi`).
- Persistent session state in SQLite.
- Sandboxed-Docker option for the sidecar's spawn (PRs `dcdda7c`/`7fffaca` already did that on the JARVIS side).
