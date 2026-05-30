# Aria → Personal Counsel Roadmap

**Status:** active tracker (single source of truth across sessions)
**Created:** 2026-05-30
**Last updated:** 2026-05-30

This document tracks the work to upgrade Aria from "voice assistant" to "personal counsel." Update status inline as items move. Each implementation PR closes one or more items here.

---

## Open architectural questions (answer before Phase 1 implementation)

| ID | Question | Status |
|---|---|---|
| Q1 | What kind of counsel? (advisor / sounding-board / friend-with-judgment / therapist-adjacent / mode-switching among all) | open |
| Q2 | Local-LLM trade-off: never-leaves-Mac vs response quality + latency. Always-local, counsel-only-local, or hybrid? | open |
| Q3 | Crisis-floor scope: which deterministic categories trigger the floor (self-harm, substance, violence, mental-health distress)? | open |
| Q4 | Vault recovery: one-time printed code, recovery questions, or accept "lose passphrase = lose Aria"? | open |
| Q5 | Idle auto-lock: minutes of inactivity threshold? Lock on WS disconnect? | open |

---

## Current state — what exists today (2026-05-30)

**Live in user's running container** (rollup of merged-locally branches):

- Voice in (Web Speech or sidecar Whisper) → LLM → voice out (Piper Cori, host-local).
- Persistent encrypted conversations (resume within 30 min — PR #25).
- Three-tier memory: rolling summary + session buffer + SQLite FTS5 facts.
- Action system: ADD_TASK, ADD_NOTE, REMEMBER, COMPLETE_TASK, GH_ISSUES_LIST/CREATE, WEB_SEARCH (Tavily), BROWSE, OPEN_TERMINAL, SCREEN, BUILD/RESEARCH/PROMPT_PROJECT (via sidecar /spawn).
- Aria persona: hyper-intelligent + warm + direct (PR #24). Permission-asking + sycophancy + moralizing banned. Ethical floor preserved (no minors, no real-world violence instructions, no non-consensual depictions).
- Vault: SQLCipher + Argon2id (256 MiB / t=3 / p=4), passphrase per restart.
- Auth token via vault, X-JARVIS-Token everywhere.
- CWD allowlist + hard-deny list + audit log on `/spawn` (PR #26).
- Untrusted-content guard against prompt injection from mail/calendar/screen.
- Loopback-only sidecar; constant-time token compare.
- Local TTS (Piper) + optional local STT (whisper) — no Google/Fish egress when sidecar installed.
- Task sidebar (live dispatch panel — PR #23).

**Open PRs (pending merge):** #22 Piper, #23 Tasks sidebar, #24 Aria persona + Tavily, #25 Persistence, #26 /spawn.

---

## Gap inventory — full count

### Functional (F)
| ID | Gap | Phase | Notes |
|---|---|---|---|
| F1 | macOS-host actions silently fail from Docker (Calendar/Mail/Notes/Terminal/Screen) | 3 | Needs AppleScript bridge in sidecar |
| F2 | No semantic memory (FTS5 keyword only) | 2 | Port `memory-lancedb` from OpenClaw |
| F3 | Rolling summary doesn't persist | 2 | Add `conversation_summaries` table |
| F4 | No cross-conversation memory | 2 | Depends on F2 |
| F5 | No proactive triggers (morning briefing, scheduled check-ins) | 3 | Cron-style scheduler |
| F6 | No document/image understanding beyond SCREEN | 3 | Port `document-extract` from OpenClaw |
| F7 | No history browse UI | 4 | Frontend panel; backend (PR #25) exposes the data |
| F8 | Voice latency stack (no streaming) | 4 | LLM streaming + chunk-based TTS |
| F9 | Stop button kills, doesn't pause/resume | 4 | LLM-side resume token |
| F10 | No multi-source synthesis on web search | 4 | Tavily returns one answer; combine multiple |
| F11 | No backup/export of vault | 1 | Part of vault recovery (see S5) |

### Personality / counsel-readiness (P)
| ID | Gap | Phase | Notes |
|---|---|---|---|
| P1 | No memory of his patterns ("what do you tend to do") | 2 | Pattern detection over persisted conversations |
| P2 | No challenge protocol when he's avoiding | 2 | Behavioral layer over persona prompt |
| P3 | No mode awareness (advise vs listen vs push) | 2 | Mode declaration system |
| P4 | No "what aren't you asking" probing | 2 | Counsel behavior layer |
| P5 | No after-action recall | 2 | Periodic check-ins on past intentions |
| P6 | No values / goals registry | 3 | Dedicated table she tracks against |
| P7 | No emotional-acuity over time | 3 | Affect tracking across sessions |

### Safety (S) — blocking for counsel role
| ID | Gap | Phase | Notes |
|---|---|---|---|
| S1 | Every conversation goes to api.anthropic.com (turn-by-turn) | **1** | **Local-LLM mode** — biggest single concern |
| S2 | No conversation deletion or expiry | **1** | DELETE endpoint + per-conversation TTL |
| S3 | No idle auto-lock (vault stays unlocked until restart) | **1** | Timer-based auto-lock |
| S4 | No secrets-detection at persist time (SSN, accounts, passwords stored + sent verbatim) | **1** | Regex pre-filter before persist + LLM |
| S5 | No vault recovery (lose passphrase = lose Aria) | **1** | Paper recovery code generated at bootstrap |
| S6 | No duress / decoy passphrase | 3 | Second passphrase → decoy vault |
| S7 | No crisis-floor detection (self-harm / substance / distress) | **1** | Deterministic intent layer with defined response |
| S8 | No memory poisoning defense (no provenance trail on REMEMBER) | 2 | Provenance column + LLM-aware origin tracking |
| S9 | Anthropic API key in-memory after unlock | 4 | Inherent to design; document explicitly |
| S10 | No "panic blur" on transcript (over-the-shoulder readable) | 3 | UI feature: blur-on-blur |
| S11 | Voice overhearable; no whisper-mode | 4 | Mute audio output, text-only mode |
| S12 | System-prompt extractable via clever prompts | 4 | Inherent; document and accept |
| S13 | Sealed-session capability (counsel sessions force local + flagged-private) | **1** | Builds on S1 |

---

## Phase 1 — Safety floor for counsel work (must-do before counsel role)

**Goal:** make the system safe enough for genuinely sensitive conversations. Implementation order matters: S1 (local LLM) and S13 (sealed sessions) are prerequisites for the counsel role being safe at all.

| # | Item | IDs closed | Spec | PR | Status |
|---|---|---|---|---|---|
| 1A | Local-LLM mode (Ollama) | S1, S13 | TBD | — | spec pending |
| 1B | Conversation deletion + auto-expire | S2 | TBD | — | spec pending |
| 1C | Idle auto-lock | S3 | TBD | — | spec pending |
| 1D | Secrets-detection redactor | S4 | TBD | — | spec pending |
| 1E | Vault paper-recovery code | S5, F11 | TBD | — | spec pending |
| 1F | Crisis-floor detection | S7 | TBD | — | spec pending |

---

## Phase 2 — Counsel behavior layer

| # | Item | IDs closed | Status |
|---|---|---|---|
| 2A | Mode declaration system | P3 | not started |
| 2B | Semantic memory (memory-lancedb port) | F2, F4, P1 | not started |
| 2C | Pattern-detection layer | P1, P5 | not started |
| 2D | Honesty contract reinforcement | P2, P4 | not started |
| 2E | Persistent rolling summary | F3 | not started |
| 2F | Memory provenance trail | S8 | not started |

---

## Phase 3 — Counsel context (richness)

| # | Item | IDs closed | Status |
|---|---|---|---|
| 3A | Calendar/Mail/Notes/Terminal/Screen bridge via sidecar | F1 | not started |
| 3B | Document understanding (PDF/image drop) | F6 | not started |
| 3C | Proactive triggers (morning briefing, scheduled check-ins) | F5 | not started |
| 3D | Goals / values registry | P6 | not started |
| 3E | After-action recall + emotional acuity tracking | P7, P5 | not started |
| 3F | Duress / decoy passphrase | S6 | not started |
| 3G | Panic-blur on transcript | S10 | not started |

---

## Phase 4 — Polish

| # | Item | IDs closed | Status |
|---|---|---|---|
| 4A | History browse UI | F7 | not started |
| 4B | Voice latency / LLM streaming | F8 | not started |
| 4C | Stop with pause-and-resume | F9 | not started |
| 4D | Multi-source web search synthesis | F10 | not started |
| 4E | Whisper-mode (audio off, text only) | S11 | not started |
| 4F | Doc/accept residual safety items | S9, S12 | not started |

---

## Workflow

1. **Tracker is canonical.** Update statuses inline as work moves.
2. **Each Phase 1 item gets its own spec** (`docs/superpowers/specs/...`) reviewed by `security-advisor` (all six are safety-relevant).
3. **Each spec gets its own implementation PR** off main, code-reviewer + test-runner gates per CLAUDE.md.
4. **Phase 2+ items also get specs** but can be sequenced after Phase 1 lands.
5. **Update the "Last updated" date at the top of this file when material progress happens.**

---

## Change log

- 2026-05-30 — Document created. Phase 1 spec generation dispatched (subagents A/B/D/F).
