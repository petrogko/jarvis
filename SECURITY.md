# JARVIS — Security Model

## Threat model in one sentence
JARVIS is a single-user, single-host voice assistant. The trust boundary
is the local machine: anything that can reach the listening socket can
drive the assistant, and the assistant can read Calendar/Mail/Notes and
spawn Claude Code sessions with full shell access. Network exposure is
disabled by default; opting in requires presenting an auth token.

## Defaults
- **Bind:** `127.0.0.1` (loopback only). Network exposure requires
  `--host 0.0.0.0` (or any other interface) explicitly.
- **Auth:** Loopback requests bypass the token (single-user case).
  Non-loopback requests must present `X-JARVIS-Token` header or
  `?token=...` query param. Token is generated in-process on first
  request after vault unlock and stored in `data/secrets.db`
  (SQLCipher). `data/.local_token` no longer exists.
- **Secrets at rest:** API keys live in `data/secrets.db` (SQLCipher).
  Bootstrap on first run via the UI lock-screen; the passphrase never
  touches disk — only the Argon2id-derived master key is held in
  memory for the lifetime of the session. The vault is locked across
  restarts; the voice loop is unavailable until unlocked.
- **CORS:** Allowlist only (`http://localhost:5173` and `http://127.0.0.1:5173`
  by default). Override with `JARVIS_CORS_ORIGINS` (comma-separated).
- **`/api/fix-self`:** Disabled unless `JARVIS_ENABLE_FIX_SELF=1` is set,
  and the request body must include `{"confirm": "rewrite-self"}`. The
  endpoint spawns a Claude Code session with `--dangerously-skip-permissions`;
  treat as full local code execution.

## Data classification

| Data                                          | Class       | At rest                                                   | In transit          |
|-----------------------------------------------|-------------|-----------------------------------------------------------|---------------------|
| `ANTHROPIC_API_KEY`, `FISH_API_KEY`, `FISH_VOICE_ID` | Secret | `data/secrets.db` (SQLCipher, Argon2id-derived key) | TLS to provider     |
| `TTS_PROVIDER`, `TTS_VOICE`                           | Secret | `data/secrets.db` (SQLCipher, Argon2id-derived key) | vault → `synthesize_speech` dispatcher only; no additional network surface |
| `auth_token`                                  | Secret      | `data/secrets.db` (was `data/.local_token` file)         | header/query        |
| `data/secrets.db`                             | Secret      | SQLCipher, key derived via Argon2id (256 MiB / t=3 / p=4); unlocked by user passphrase on every container start | n/a |
| `data/kdf.salt`                               | Public      | 16 bytes random, mode 0644; public by design             | n/a                 |
| `data/jarvis.db` (memory + tasks)             | PII         | SQLCipher, same master key as `secrets.db`               | n/a                 |
| `data/jarvis.db.pre-encrypt.bak`, `data/.env.bootstrap.pre-encrypt.bak` | Sensitive | Plaintext migration backups, mode 0600; auto-deleted on second successful unlock after migration | n/a |
| `data/audit.jsonl`                            | Internal    | Deliberately plaintext — forensic preservation (see note below) | n/a       |
| Sidecar token                                 | Secret      | `~/Library/Application Support/jarvis-sidecar/token` (mode 0600) | `X-SIDECAR-Token` header on loopback only |
| Voice audio bytes (STT requests)              | Transient PII | Never persisted by JARVIS; temporarily in macOS user-owned tmpdir on sidecar (cleaned via `finally` on every request) | in-memory during `/api/stt` request only |
| Calendar / Mail / Notes content               | PII         | OS apps                                                   | osascript stdout    |
| Cost telemetry (`data/usage.jsonl`)           | Internal    | local                                                     | n/a                 |
| Session token counters                        | Internal    | in-memory                                                 | `/api/usage` (auth) |

> **Note on `data/audit.jsonl`:** The file is deliberately plaintext and append-only. The passphrase is the single root of trust; if audit logs were also encrypted, a lost passphrase would destroy the forensic record needed for incident response. An attacker with disk access can read it; an attacker who steals only the encrypted DBs cannot. Operators concerned about audit-log confidentiality should rely on FileVault as defense in depth.

## TTS egress

Fish Audio was the only TTS path pre-wave-1. As of `openclaw_ports/tts_local_cli` (MIT, ported from OpenClaw), macOS host installs default to local `say` for TTS — no third-party egress for the audio. The Docker container still falls back to Fish Audio because Linux lacks `say`. Provider chosen by vault key `TTS_PROVIDER` ∈ {auto, local_cli, fish_audio, sidecar}; default 'auto' tries local first, falls back to Fish. When `TTS_PROVIDER=sidecar` (or `auto` + sidecar available), no Fish Audio egress occurs — audio is synthesized by the host sidecar via `say`.

## STT egress

By default, Chrome Web Speech sends audio to Google. When `STT_PROVIDER=whisper`, voice audio is POSTed to `/api/stt` on the JARVIS server and forwarded to the host sidecar, which runs `whisper-cli` locally. Voice audio never leaves the local machine. This replaces the Chrome Web Speech ↔ Google path entirely.

## Trust boundaries

| Boundary | Transport | Auth |
|---|---|---|
| Browser → JARVIS server | WebSocket / HTTP, loopback | `X-JARVIS-Token` (non-loopback only) |
| JARVIS server → Anthropic | TLS | API key from vault |
| JARVIS server → Fish Audio | TLS | API key from vault |
| JARVIS server → macOS apps | `osascript` argv | n/a (runs as same user) |
| JARVIS server → Claude Code subprocess | `claude -p` | inherits user permissions; triple-gated |
| **JARVIS Docker → host sidecar** | **loopback HTTP (`host.docker.internal:9999`)** | **`X-SIDECAR-Token`; never reaches public internet** |

## What is intentionally NOT defended against
- Another process running as the same macOS user can read the sidecar token file. `chmod 600` + user-account separation is the only defense — same boundary as `data/secrets.db` for the vault.
- Compromised host-resident sidecar means transcripts/audio could be leaked locally. The sidecar's code is in this repo; auditable.
- A user with a shell on the JARVIS host. The server runs as that user
  and can do anything they can do.
- A user who explicitly sets `--host 0.0.0.0` and shares the token, or
  sets `JARVIS_TRUST_LOOPBACK=0` on a multi-user machine. Those are
  affirmative choices.
- Compromise of Apple Calendar/Mail/Notes themselves — read paths are
  read-only by design; write paths are limited to Notes creation.
- **Passphrase loss equals permanent data loss.** There is no recovery
  path. Keep an out-of-band backup of the passphrase.
- **Remote brute-force when `--host 0.0.0.0` is used.** An attacker
  with LAN access can attempt the unlock endpoint at the rate-limited
  rate of 1 attempt / 2 seconds. The Argon2id cost makes each attempt
  slow; passphrase strength is the load-bearing defense. The UI
  enforces zxcvbn ≥ 3 at bootstrap as a guardrail, but that check is
  client-side only.

## Subprocess sandboxing — `claude -p`

Five sites in the codebase launch `claude -p --dangerously-skip-permissions`:
research, work-mode, QA verify, QA auto-retry, and the visible
Terminal task spawned by `POST /api/tasks`. Each spawn is gated by:

1. `claude_pool` — global semaphore caps concurrent processes (env
   `JARVIS_MAX_CONCURRENT_CLAUDE`, default 5).
2. `cwd_allowlist` — resolved cwd must be inside `~/Desktop`, the
   JARVIS repo, or a path listed in `JARVIS_EXTRA_PROJECT_DIRS`.
3. `audit_log` — every spawn (success, cwd-reject, validator-reject)
   is appended to `data/audit.jsonl`.

For the four background sites a fourth layer is available: each
spawn can run inside an ephemeral Docker container instead of
directly on the host. Set `JARVIS_CLAUDE_RUNNER=docker` after
building the image:

```
docker build -t jarvis-claude:latest docker/claude
JARVIS_CLAUDE_RUNNER=docker python server.py
```

The container has only the project directory mounted (`-v
${cwd}:/work:rw`), 2 GiB memory cap, 1 CPU, non-root user inside,
and `--rm` (no persistent state between spawns). Auth is via the
host's `ANTHROPIC_API_KEY` env var passed through (`-e
ANTHROPIC_API_KEY`); your Claude Code subscription login is never
mounted into the container. Trade-off: ~1–2 sec startup per spawn,
and Claude Code Pro features that depend on the local session are
unavailable inside the sandbox.

The fifth site (`POST /api/tasks` → visible Terminal window) stays
on `direct` regardless, because the UX is a Terminal window the
user watches — Docker can't render that. The `cwd_allowlist` is
the only sandbox for that site.

## AppleScript injection
All AppleScript invocations that interpolate runtime values pass those
values via `osascript` argv (`item N of argv` inside `on run argv`),
never via f-string interpolation into the script source. The shell-exec
primitive (`do script`) is reachable only through call sites whose
inputs are either literal constants or regex-restricted (see
`_assert_safe_path` in `actions.py`).

## Reporting
This is a personal project. For coordinated disclosure of issues that
could affect anyone running the public repo, open a private issue or
email the repo owner. Do not file public issues for unpatched
vulnerabilities.

## Operator's checklist before exposing on LAN
1. **Set a strong passphrase on first run; the UI enforces zxcvbn ≥ 3.**
2. **Keep an out-of-band backup of the passphrase. There is NO recovery.**
3. Run with `--host 0.0.0.0` (or specific interface).
4. Unlock the vault via the UI lock-screen on first run. The token is
   then available via `/api/auth/state` (auth required); it is no
   longer written to `data/.local_token`.
5. Configure the remote client to send `X-JARVIS-Token: <token>`
   on REST and `?token=<token>` on the WebSocket URL.
6. Restrict `JARVIS_CORS_ORIGINS` to the remote frontend origin only.
7. Consider whether `/api/fix-self` should be enabled (default: no).
8. Prefer HTTPS — drop `cert.pem`/`key.pem` next to `server.py` and
   the server auto-enables TLS.
9. After the first unlock, the **second** successful unlock automatically
   clears the plaintext migration backups (`*.pre-encrypt.bak`). You
   can also clear them manually via the settings UI.
