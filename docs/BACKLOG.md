# JARVIS Backlog

Living tracker for in-flight and pending work. Each entry: short rationale + status + routing per CLAUDE.md persona table.

**Workflow:** non-trivial items must be specced via `superpowers:writing-plans` and reviewed by the right persona (software-architect for design, security-advisor for trust-boundary or membrane changes) **before** implementation. See [[feedback-use-superpowers]] in memory.

---

## Priority queue (next session)

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

_(none — vault branch `feat/ui-config-encrypted-2026-05` is in review)_

---

## Done (recent)

- **Sidecar `/spawn` (PR #pending):** the host sidecar gains a `POST /spawn` endpoint that runs `claude -p --dangerously-skip-permissions` on the macOS host. Unblocks `[ACTION:BUILD]`, `[ACTION:RESEARCH]`, and `[ACTION:PROMPT_PROJECT]` from the JARVIS Docker container (no `claude` CLI in the container; no host shell). `claude_runner` auto-detects `/.dockerenv` and switches to the `sidecar` backend by default. Workdir allowlist + hard-deny list + per-minute rate cap + concurrency cap + soft/hard output caps + process-group isolation + audit log per spec `docs/superpowers/specs/2026-05-29-sidecar-spawn-design.md` (security-advisor GO-WITH-FIXES, all 7 required fixes folded in).
- **P3a + P13 merged (PR #20):** jarvis-sidecar — combined macOS host daemon exposing /tts (wraps `say`) and /stt (wraps `whisper-cli`). Eliminates the last two cloud voice egresses (Google Web Speech for STT, Fish Audio for TTS when in Docker). New vault keys: `STT_PROVIDER`, `SIDECAR_URL`, plus `TTS_PROVIDER=sidecar` value. Sidecar installs via `host-sidecar/setup.sh` + launchctl.
- **P11 wave-1 port 2: `gh_issues`** (PR #18): `[ACTION:GH_ISSUES_LIST owner/repo]` + `[ACTION:GH_ISSUE_CREATE owner/repo|title|body]`. Vault key `GITHUB_TOKEN`. MIT-attributed to OpenClaw commit `125d82c`. Works in Docker (pure HTTPS to `api.github.com`).
- **P11 wave-1 port 3: `apple_notes` — SKIPPED with rationale.** OpenClaw's skill is a markdown wrapper around the third-party `memo` brew CLI. JARVIS's existing `notes_access.py` already covers read/search/create/list-folders via AppleScript. OpenClaw's net-add (delete/edit/move/export) overlaps with destructive operations that JARVIS deliberately doesn't support (same safety stance as Mail being read-only). Move/export alone deemed insufficient to justify an external CLI dependency.
- **Frontend UX bundle (PRs #16 + #17):** type-to-JARVIS text input, transcript conversation panel showing USER + JARVIS lines, backend always emits text (even when no audio bytes), browser speechSynthesis fallback, voice picker dropdown, stop button, mic-default-off.
- **P11 wave-1 port 1 (= P3 privacy win):** `tts_local_cli` ported under `openclaw_ports/` (MIT, attributed to OpenClaw commit `125d82cab2952f87f532106a368d54e526141026`). macOS host now uses local `say` for TTS by default; Fish Audio remains as automatic fallback. New vault keys: `TTS_PROVIDER`, `TTS_VOICE`. Eliminates third-party TTS egress on macOS.
- **P1 + P2** merged into a single rollout in `feat/ui-config-encrypted-2026-05`: SQLCipher-encrypted dual-DB vault (Argon2id KDF), UI lock-screen, safe legacy migration, auth tokens moved to vault, settings + memory endpoints routed through vault. (P1+P2 merged into a single rollout in feat/ui-config-encrypted-2026-05.)
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
