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
   - Roots = `~/Desktop` ONLY by default. `JARVIS_EXTRA_PROJECT_DIRS` (comma-separated env var) may add roots opt-in. **`~/Development` is NOT in the default allowlist** (per advisor §11.2 — typically holds repos with secrets/deploy keys).
   - The JARVIS repo root is NOT in the allowlist — sidecar must never spawn claude inside itself or inside JARVIS's own repo.
   - **Hard deny list (required fix #2)** — these paths are rejected even if a future `JARVIS_EXTRA_PROJECT_DIRS` would otherwise admit them:
     - `~` (user home itself)
     - `~/Library`
     - `~/.ssh`, `~/.aws`, `~/.config`, `~/.gnupg`, `~/.kube`, `~/.docker`
     - any path containing a `.env` component (e.g. `.env`, `.env.local`, `.envrc`)
     - any path containing a `.git` component (the dir itself; subtree git repos are allowed)
   - Resolve via `Path(p).expanduser().resolve(strict=False)` BEFORE matching, so `..` and symlinks are flattened.
   - **Required fix #1** — after resolve, the workdir itself MUST NOT be a symlink (`Path.is_symlink()` on the original input). Subtree symlinks under the workdir ARE accepted — same as today's `claude_runner` posture; documented as an explicit limitation.
   - Workdir must EXIST as a directory.
   - Rejection → 400, audit log entry written (verb=`reject`, reason set).
2. **Prompt size cap** — `PROMPT_MAX_BYTES = 64 * 1024` (64 KiB). Larger → 400.
3. **Prompt via stdin pipe** — `asyncio.create_subprocess_exec` with `stdin=PIPE`, `await proc.communicate(input=prompt_bytes)`. No shell, no temp file, no f-string interpolation. (`claude_runner.py` precedent: "no leakage to `ps`, no length limit".)
4. **Argv allowlist** — exact list, no user-controlled flags:
   ```python
   argv = ["claude", "-p", "--output-format", "text", "--dangerously-skip-permissions"]
   ```
5. **Agent allowlist** — only the literal string `"claude"` accepted in the request body. Anything else → 400. Reserved for future expansion.
6. **Timeout** — `timeout_s` clamped to [60, 1800]. Default **300** (lowered from 600 per advisor recommendation). SIGTERM then SIGKILL on expiry. Status → `timeout`.
7. **Concurrency cap + per-minute rate (required fix #3)** — `SPAWN_MAX_CONCURRENT = 3` simultaneous spawns AND `SPAWN_MAX_PER_MINUTE = 10` rolling-window cap on spawn-creates. Either limit hit → 429. Rate cap defends against an injection that loops short spawns and evades the concurrency cap.
8. **Output cap + hard cap** — soft `OUTPUT_MAX_BYTES = 1 * 1024 * 1024` (1 MiB) — output beyond is dropped, response carries `output_truncated: true`. **Circuit breaker (advisor recommendation):** `OUTPUT_HARD_CAP_BYTES = 4 * 1024 * 1024` (4 MiB) — if the child overruns 4× the soft cap, the process group is killed; status → `killed`, reason `output_overrun`.
9. **No-prompt-in-logs invariant (required fix #5)** — the prompt bytes MUST NOT appear in any sidecar log line, including stack traces from exception paths. A dedicated test asserts this by raising mid-spawn and grepping the captured log buffer for the canary string.
10. **Audit log entry per spawn (required fix #4)** — appended to `~/Library/Logs/jarvis-sidecar.log` as a single JSON line. Fields:
    - `ts` (ISO8601), `verb` (`spawn`/`reject`/`finish`/`timeout`/`killed`/`delete`),
    - `session_id` (uuid4),
    - `caller_fingerprint` (first 8 hex of `sha256(token_bytes)` — distinguishes callers if token ever changes),
    - `workdir` (resolved),
    - `prompt_bytes` (size only — never content),
    - `exit_code` (when terminal),
    - `status`, `duration_ms`.
    Prompt content is NEVER logged. DELETE/kill events also emit a line (`verb=killed`, `by_caller=true`).
11. **Token auth** — same `require_token` dependency that `/tts` and `/stt` use. Constant-time compare via `auth.header_matches`.
12. **TTL eviction** — finished sessions stay in the registry for `SESSION_TTL_S = 300` (5 minutes) for status polling. After TTL, they're evicted and `GET /spawn/{id}` returns 404.
13. **Resource limits** — sidecar process already constrained by macOS user limits; no rlimit/cgroup change in this PR.
14. **Process group isolation (required fix #7)** — every spawn is created with `start_new_session=True` (POSIX setsid), so claude becomes its own session leader. SIGTERM/SIGKILL on timeout, DELETE, and `output_overrun` is delivered to the **entire process group** (`os.killpg(os.getpgid(proc.pid), signal)`), not just claude's PID. This reaps MCP servers, child shells, and any subprocesses claude spawned. Without this, a DELETE would leave orphans.
15. **`/health.spawn_ready` is authed (required fix #6)** — `/health` already sits behind `require_token` in `app.py`; the new `spawn_ready: bool` field MUST stay behind that gate. If we ever expose `/health` unauthed for liveness probes, `spawn_ready` must be omitted from the unauthed branch.

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
OUTPUT_MAX_BYTES: Final[int] = 1 * 1024 * 1024            # soft cap; mark truncated
OUTPUT_HARD_CAP_BYTES: Final[int] = 4 * 1024 * 1024       # circuit breaker; kill group
SPAWN_MAX_CONCURRENT: Final[int] = 3
SPAWN_MAX_PER_MINUTE: Final[int] = 10                     # rolling-window rate cap
SPAWN_DEFAULT_TIMEOUT_S: Final[float] = 300.0             # was 600; advisor recommended
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
- **Hard-deny rejection** — workdirs under `~/Library`, `~/.ssh`, `~/.aws`, `~/.config`, a path containing `.env`, a path containing `.git` → 400, even when `JARVIS_EXTRA_PROJECT_DIRS` would otherwise admit them.
- **Symlink-as-workdir rejection** — pass a symlink whose target is allowlisted → 400 (workdir input itself is a symlink).
- Workdir does-not-exist → 400.
- Prompt too large (> 64 KiB) → 400.
- Unknown agent → 400.
- Timeout out of range → 400.
- Concurrency cap → 4th simultaneous spawn returns 429.
- **Per-minute rate cap** — 11th spawn within a 60s rolling window returns 429 even when no concurrent sessions are active.
- Output soft truncation — fake subprocess emits > 1 MiB; final response has `output_truncated: true`, length exactly `OUTPUT_MAX_BYTES`.
- **Output hard-cap circuit breaker** — fake subprocess emits > 4 MiB; process group is killed, status `killed`, reason `output_overrun`.
- Hard timeout — fake proc that never exits is killed at `timeout_s`; status `timeout`.
- **Group kill on timeout/DELETE** — spawn a fake parent that itself spawns a child; verify the child is reaped via SIGKILL on the group (not orphaned).
- DELETE kills a running session, audit log emits `verb=killed by_caller=true`, response carries final state.
- GET unknown session → 404. GET expired session → 404 (after TTL).
- /health behind `require_token`; unauthed → 401. Authed → includes `spawn_ready` based on `shutil.which("claude")`.
- **No-prompt-in-logs invariant** — force an exception mid-spawn with a canary prompt string; assert the captured log buffer contains neither the canary nor any prompt-bytes substring.
- **Audit-log line shape** — assert each spawn writes one JSON line containing `session_id`, `caller_fingerprint`, `workdir`, `prompt_bytes` (size only), `status`, `duration_ms`; assert prompt content does NOT appear in the line.
- Source-scan test: `spawn.py` MUST NOT use `shell=True`, `os.system`, or `subprocess.run` without an explicit argv list. Argv list constructed without f-string interpolation.

JARVIS-side: `test_sidecar_client.py` — `spawn_via_sidecar` POSTs the right body shape, returns dict on 200, None on errors.

## 9. Documentation

- `SECURITY.md`: new "Sidecar /spawn" subsection documenting the workdir allowlist (canonical list), the hard-deny list, prompt/output caps, per-minute rate cap, token auth, the `--dangerously-skip-permissions` implication, and the "single-user Mac, host-shell-attacker out-of-scope" framing. **Required note (advisor recommendation):** explicitly state that the prompt comes from LLM-classified intent, that a prompt-injection in untrusted content (email/calendar) can reach claude verbatim, and that the workdir allowlist is the only structural guard on what claude does on disk. Do not let future readers assume the sidecar sanitizes.
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

## 11. Open questions — RESOLVED by security-advisor (GO-WITH-FIXES)

1. ✅ **Flag name** — keep `--dangerously-skip-permissions`. Matches `claude_runner.py` for grep parity; the word "dangerously" carries operator-UX weight.
2. ✅ **`~/Development` default** — **OUT**. Strictly opt-in via `JARVIS_EXTRA_PROJECT_DIRS`. Default allowlist is `~/Desktop` only. (Folded into §5.1.)
3. ✅ **Audit log destination** — single `~/Library/Logs/jarvis-sidecar.log`, structured JSON lines. Don't fragment.
4. ✅ **Prompt cap** — 64 KiB stays. Lower would break legitimate large-context BUILD prompts; very large contexts belong in files within the workdir, not stuffed in the prompt.
5. ✅ **Per-token rate limit** — required. `SPAWN_MAX_PER_MINUTE=10` rolling-window added (§5.7).
6. ✅ **Output redaction** — NO. Trust boundary (loopback + token + bounded buffer + single-user-Mac) is the right answer; redaction is false-confidence.

### Required fixes applied to this spec
1. ✅ Workdir-must-not-be-a-symlink check + subtree-symlinks-accepted limitation documented (§5.1).
2. ✅ Hard-deny list (`~`, `~/Library`, `~/.ssh`, `~/.aws`, `~/.config`, `~/.gnupg`, `~/.kube`, `~/.docker`, any `.env` or `.git` component) (§5.1).
3. ✅ Per-minute spawn budget `SPAWN_MAX_PER_MINUTE=10` alongside concurrency cap (§5.7, §6.3).
4. ✅ Audit log fields extended with `session_id`, `caller_fingerprint` (first 8 hex of sha256), and DELETE/kill events (§5.10).
5. ✅ No-prompt-in-logs invariant + test required (§5.9, §8).
6. ✅ `/health.spawn_ready` confirmed behind `require_token` (§5.15).
7. ✅ Process group isolation — `start_new_session=True` + `killpg` on the group (§5.14).

### Recommendations folded in
- Default timeout reduced 600 → 300 (§5.6).
- `OUTPUT_HARD_CAP_BYTES=4 MiB` circuit breaker (§5.8).
- Group-kill test with fake grandchild (§8).
- SECURITY.md prompt-injection acknowledgment note (§9).

## 12. Out-of-scope follow-ups

- SSE/WebSocket streaming.
- Multi-agent (`codex`, `opencode`, `pi`).
- Persistent session state in SQLite.
- Sandboxed-Docker option for the sidecar's spawn (PRs `dcdda7c`/`7fffaca` already did that on the JARVIS side).
