# JARVIS Backlog

Living tracker for in-flight and pending work. Each entry: short rationale + status + routing per CLAUDE.md persona table.

**Workflow:** non-trivial items must be specced via `superpowers:writing-plans` and reviewed by the right persona (software-architect for design, security-advisor for trust-boundary or membrane changes) **before** implementation. See [[feedback-use-superpowers]] in memory.

---

## Priority queue (next session)

### P1 — UI-only configuration (no `.env` on host)
**Status:** proposed
**Persona routing:** `software-architect` (refactor across `server.py` + new storage module + Docker compose; changes trust boundary for secret storage) → `security-advisor` (touches secrets-at-rest) → implementation → `code-reviewer` → `test-runner`.

**Why:** today secrets live in `.env` on the host. UI saves work in-process but Docker reload doesn't pick them up cleanly (compose `env_file` is boot-only; in-container `.env` writes go to a non-bind-mounted layer). User wants UI-only flow.

**Sketch (architect to validate):**
- Move config storage to `data/settings.json` (already bind-mounted via `./data:/app/data:rw`).
- `_read_env` / `_write_env_key` swap to read/write that file instead of `.env`.
- `.env` becomes optional bootstrap-only (first-run convenience), gitignored, never required.
- Server reads on every request → no restart needed after UI save.
- File perms hardened (chmod 600) by existing `file_perms.harden_secrets_at_startup`.

---

### P2 — Memory hardening (SQLCipher + PII redactor)
**Status:** proposed (user-selected priority before UI-config emerged)
**Persona routing:** `software-architect` → `security-advisor` (membrane: data-at-rest classification) → implementation → review/test.

**Sketch:**
- Migrate `memory.py` SQLite to SQLCipher; encryption key stored in `data/settings.json` (depends on P1) or macOS Keychain on host.
- Add `pii_redactor.py`: regex + heuristic strip of emails, phones, credit cards, SSN-shaped tokens from `[ACTION:REMEMBER]` payloads before insert.
- Migration tooling for existing `data/memory.db` (single-user, low risk, but still: backup + dry-run).

**Depends on P1** for key storage.

---

### P3 — Privacy: local Whisper STT + macOS `say` TTS
**Status:** proposed
**Persona routing:** `software-architect` (touches voice-loop trust boundary; STT moves from browser→backend) → implementation → review/test.

**Why:** today Chrome Web Speech sends audio to Google; Fish Audio receives JARVIS's response text. Both eliminated by going local.

**Sketch:**
- **TTS:** swap Fish Audio call site (`server.py` ~line 1211–1220) for macOS `say -o /tmp/jarvis.aiff -v <voice>` → return audio bytes. Host-only path; Docker container can't run `say`. Document the fallback.
- **STT:** frontend captures raw PCM via MediaRecorder → POSTs to `/api/stt` → backend runs `faster-whisper` or `whisper.cpp` (small or base model). Adds a Python dep — audit.
- Voice fidelity drops (no MCU-JARVIS Fish voice); user has accepted this trade.

---

### P4 — Egress sidecar (kernel-level network gate)
**Status:** documented in `docs/DOCKER.md` as future work
**Persona routing:** `software-architect` → `security-advisor` → implementation.

**Sketch:** tinyproxy or squid sidecar in `docker-compose.yml`, backend `HTTPS_PROXY` env points at it, backend's outbound default route dropped. Allowlist: `api.anthropic.com`, `api.fish.audio` (or local-only after P3).

---

### P5 — Memory recall via semantic search
**Status:** proposed
**Persona routing:** `software-architect`.

**Sketch:** add embeddings column to memory table; on REMEMBER store embedding (Anthropic embeddings or local model); on RECALL use cosine over embeddings then FTS5 for hybrid. Massive UX win for "remember when I…".

---

### P6 — Background daily briefing (proactive)
**Status:** proposed
**Persona routing:** `software-architect` (new module; introduces cron-style timer).

**Sketch:** at user-configured time, JARVIS reads calendar + mail + tasks, generates a one-sentence briefing, plays via TTS. Opt-in. Should respect mute state.

---

### P7 — Action approval queue
**Status:** proposed
**Persona routing:** `software-architect` → `security-advisor`.

**Sketch:** `[ACTION:BUILD]` / `[ACTION:BROWSE]` / `[ACTION:PROMPT_PROJECT]` dispatches queue for one-click UI approval on first use per session (or always-on mode). Reuses `audit_log`.

---

### P8 — `[ACTION:RESEARCH]` with citations + cache
**Status:** proposed
**Persona routing:** `software-architect`.

**Sketch:** structured citations on Playwright research results; cache the page-fetch layer with a TTL. Needs container variant that ships Playwright (separate image — `Dockerfile.browser` per DOCKER.md).

---

### P9 — Network bind-LAN guardrail
**Status:** proposed
**Persona routing:** `security-advisor`.

**Sketch:** `--host 0.0.0.0` should require a flag + interactive confirmation + a warning logged to audit. Today it's allowed silently if you pass it.

---

### P11 — OpenClaw audit + bridge POC
**Status:** proposed (deferred until vault PR lands)
**Persona routing:** `software-architect` (new integration surface).

**Why:** OpenClaw (`/Users/petrog/Development/github/openclaw`) is an MIT-licensed multi-channel personal-assistant framework with 50+ extensions (`apple-notes`, `github`, `gh-issues`, `1password`, etc.). Bridging select extensions into JARVIS as `[ACTION:X]` backends could massively expand capabilities without rewriting JARVIS.

**Sketch (post-vault):**
1. Audit `openclaw/security/`, `openclaw/skills/`, `packages/plugin-sdk/`, `src/gateway/protocol/` — document what JARVIS could learn or borrow (license-permitting).
2. Pick one extension as a POC (`apple-notes` is the cleanest test case — Node-side, no AppleScript collision since it goes through OpenClaw's own bridge).
3. Spec a JS↔Python bridge: JARVIS dispatches `[ACTION:NOTE_FROM_OPENCLAW]` → spawns the OpenClaw extension via its plugin SDK → captures result.

**Blocker:** subagent file-access permissions to `/Users/petrog/Development/github/openclaw`. Either grant via `/permissions` or do the audit in-thread.

---

### P12 — WorldMonitor integration: news + geopolitical briefings
**Status:** proposed
**Persona routing:** `software-architect` (new external dependency + trust-boundary).

**Why:** WorldMonitor (`/Users/petrog/Development/github/worldmonitor`, also hosted at worldmonitor.app) is a real-time global intelligence dashboard with 500+ news feeds + AI synthesis. JARVIS could call its public HTTP API to answer "what's happening in <region/topic>" voice queries.

**⚠ License caveat:** WorldMonitor is **AGPL v3** (strong copyleft). JARVIS **cannot vendor their code** without itself becoming AGPL. JARVIS **can** call their public HTTPS endpoints (network linking, not derivative work — long-standing FOSS interpretation). Integration MUST be network-only.

**Sketch:**
1. Verify WorldMonitor exposes a stable public REST API (check their docs at worldmonitor.app/docs/documentation; if no public API, scope-down or request access).
2. New action: `[ACTION:NEWS topic="..."]` and `[ACTION:WORLD region="..."]`. Handler hits the API, passes results through `untrusted_content.sanitize` + `wrap` (the API response is third-party untrusted content), feeds to Claude for butler-tone synthesis.
3. Add API key (if required) to the vault `secrets` table per P1.

---

### P10 — Persona invocation from voice loop
**Status:** proposed (speculative)
**Persona routing:** `software-architect`.

**Sketch:** voice command "JARVIS, security-audit the current diff" → JARVIS spawns the security-advisor persona via the dev-session subagent infrastructure and reads its report back. Bridges dev-time personas to runtime.

---

## In flight

_(none — last branch `feat/personas-design-2026-05` is in review at #9)_

---

## Done (recent)

- 5-persona dev-session infrastructure + tripwire hook + CLAUDE.md routing (PR #9, branch `feat/personas-design-2026-05`)
- Goal-drift integration test (same PR)
- Docker setup for backend with audited egress allowlist (same PR)
- 8 hardening PRs (#1–#8): loopback default, token auth, AppleScript injection closed, untrusted content sanitizer, claude -p sandbox + cwd allowlist + audit log, file perms hardening, pip-audit in CI, Docker sandbox for claude -p

---

## Ground rules (do not lose)

1. **Personas first.** Per CLAUDE.md routing — software-architect before non-trivial code; security-advisor for membrane touches.
2. **Plan before code.** Use `superpowers:writing-plans` for any multi-step item. Plans live in `docs/superpowers/plans/`.
3. **Test before claim.** `test-runner` persona before any "ready to merge" assertion.
4. **One PR per item.** Bundle related work; don't smear concerns across PRs.
5. **Update this file** when items change status. Move done items to the bottom; trim after 10.
