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
| `TTS_ENGINE` (∈ {say, piper})                         | Secret | `data/secrets.db` (SQLCipher, Argon2id-derived key) | vault → sidecar `/tts` engine selector; no additional network surface |
| `TTS_PIPER_VOICE` (default `en_GB-alan-medium`)       | Secret | `data/secrets.db` (SQLCipher, Argon2id-derived key) | vault → sidecar `/tts` argv (validated); no additional network surface |
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

## Piper TTS engine (sidecar)

The host sidecar's `/tts` endpoint has two engines, selected by vault key
`TTS_ENGINE` ∈ {say, piper} (default `say`). Piper (OHF-Voice/piper1-gpl)
is **GPL-3.0** and is never imported by JARVIS or the sidecar: it lives in
an *isolated* Python venv under the sidecar state dir and is invoked
**only as a subprocess** (`python -m piper`) — same arm's-length boundary
as `say`/`whisper-cli`/`ffmpeg`, so the GPL does not reach JARVIS's MIT
code. Install is opt-in (`./host-sidecar/setup.sh --with-piper`).

Guards:
- The voice name (`TTS_PIPER_VOICE`, default `en_GB-alan-medium`) is
  validated against `^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$` before use. This
  is an argv-injection guard: the voice flows into `piper` argv **before**
  the `--` separator, so a leading-dash name could otherwise be parsed as
  a piper flag.
- Piper input text is capped at 2000 chars.
- Voice models are SHA256-pinned at setup time (the pin slot in
  `setup.sh` warns if unset).
- `/tts` falls back to `say` when piper is unavailable; `/health` reports
  `piper_available`.

## STT egress

By default, Chrome Web Speech sends audio to Google. When `STT_PROVIDER=whisper`, voice audio is POSTed to `/api/stt` on the JARVIS server and forwarded to the host sidecar, which runs `whisper-cli` locally. Voice audio never leaves the local machine. This replaces the Chrome Web Speech ↔ Google path entirely.

## Safety floor (crisis_floor)

Phase-1 hardening per `docs/superpowers/specs/2026-05-30-crisis-floor-design.md`. A deterministic regex filter (`crisis_floor.py`) runs on every user turn **before** `generate_response`. The scanner refuses bare strings — it requires a typed `UserTurn` object — so a routing mistake cannot slip untrusted-content (mail/calendar quotes) into the detector by accident.

This is **NOT** a content-moderation layer and **NOT** a clinical tool. It is a small reliable floor that ensures: when explicit self-harm ideation, active substance crisis, or acute panic statements occur, Aria's response is deterministic (ground-validate-refer-stay) rather than whatever the LLM happens to generate. The advisor's "tiny local classifier" was dropped — regex-only with sentence-level negation handling is simpler and audit-clean.

**Tier 1 (auto-response):** `ideation` / `substance` / `panic` / `self_harm_method`. Bypasses `generate_response`; the WS handler emits a `crisis_floor_response` frame with `neutral_voice: true` so the frontend can render with a distinct voice — Aria's warm Cori delivering "988 lifeline is there" is tonally wrong and possibly harmful.

**Tier 2 (context flag only):** indirect distress signals. Aggregated into a daily counter, never logged per-event. A `TIER2_PERSONA_FLAG` is injected into Aria's system context with explicit "do not reveal" instructions so she adjusts behavior without disclosing the flag.

**Suppression** (sentence-level, not 5-token window):
- Negation token anywhere earlier in the same sentence — "I would never want to die," "I would never, after everything I've been through this year, want to kill myself."
- Temporal-past markers anywhere in the sentence — "I used to want to die," "back when I was drinking I wanted to die."
- Fiction/quote markers — "character / lyrics / song / movie / she said / he said."

**Vault keys:** `CRISIS_FLOOR_MODE` ∈ {`on`, `tier2_only`, `off`}, default `on`; `CRISIS_FLOOR_LOCALE` ∈ {`us`, `gb`, `intl`}, default `us`.

**i18n:** Response templates are keyed by `(category, locale)`. US: 988 Suicide and Crisis Lifeline + 911 + Poison Control. GB: Samaritans 116 123 + 999 + NHS 111. INTL: Befrienders Worldwide pointer.

**Audit:** Tier 1 entries record `(tier, category, conversation_id, expires_at)` — never the matched text, never user content. Tier 1 entries auto-expire after 30 days so a follow-up CLI can purge them without touching the rest of the audit log. Tier 2 is aggregated daily; per-event distress-suspicion logging is intentionally absent.

**Post-filter:** `scan_assistant_output()` checks Aria's generated text for `substance` and `self_harm_method` categories only (where Aria authoring has no legitimate counsel use). `ideation` and `panic` in Aria's output stay log-only because roleplay is plausible.

**Known limitations:** Idiomatic "made me want to die" / "dying over here" as exhaustion or dark humor — the suppression doesn't cover these yet; some idiomatic FPs may still fire. Documented as a tightening target for Phase 2. The floor responses are written in plain calm tone; no warmth markers, no Aria persona phrases.

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
