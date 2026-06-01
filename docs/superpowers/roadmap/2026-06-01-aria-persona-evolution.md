# Aria Persona Evolution — Tracker

**Status:** active (single source of truth across sessions)
**Created:** 2026-06-01

Tracks the work to evolve Aria from "witty British secretary" to "your Aria" — a personalized, mode-aware, opinionated counsel-grade voice. Updates inline as items move.

---

## Six categories — what changes the feel of talking to her

### 1. Personal vs. generic ← **biggest lever, highest priority**

Right now she's a witty British secretary who could be anyone's. The persistence layer remembers conversations but she doesn't actively use that memory to be yours specifically. Subitems:

| ID | Item | Status |
|---|---|---|
| 1.1 | Address user by name (use `USER_NAME` vault key), alternate with "sir" | this PR |
| 1.2 | Inject memorable past lines into system prompt (recent conversations) | this PR |
| 1.3 | Opening turn on WS connect — observation, not silent waiting | done (PR #34) |
| 1.4 | Time-since-last-conversation context (`"you were here three hours ago"`) | this PR |
| 1.5 | Recurring-thread surfacing — track which topics keep coming up | follow-up (needs embeddings) |
| 1.6 | Reference specific past conversations by content | this PR |

### 2. Reads the room (replaces explicit mode-switching)

Original plan: explicit modes (advisor / sounding-board / friend / counsel) selected via vault key + UI dropdown. **Rejected** after user feedback ("why should we pick modes?"). A real person reads the room and adjusts implicitly. Aria should do the same. Made the LLM do the adjusting; gave it the criteria.

| ID | Item | Status |
|---|---|---|
| 2.1 | `ARIA_MODE` vault key — **deprecated**; no longer injected into prompt | done |
| 2.2 | Settings UI mode dropdown — **removed** | done |
| 2.3 | "HOW YOU READ THE ROOM" prompt section with 6 register-shift criteria | done |
| 2.4 | Auto-detected mode shifts → load-bearing trust in the LLM via the prompt | done |

### 3. Real opinions and friction

She agrees with everything; that's a friendly-mirror failure mode. A persona that pushes back when warranted is sharper.

| ID | Item | Status |
|---|---|---|
| 3.1 | Specific tastes (books, music, food she'd mention by name) | this PR |
| 3.2 | Specific micro-refusals ("I will not pretend to be interested in X, sir") | this PR |
| 3.3 | Explicit "when you're wrong, name it" guidance | this PR |
| 3.4 | A few signature recurring motifs / running jokes | this PR |

### 4. Embodiment depth

The current photo + audio amplitude pulse is the floor. Phase 2 of the avatar:

| ID | Item | Status |
|---|---|---|
| 4.1a | Formant-driven mouth shape (F1 openness + F2 width) — browser-only, no sidecar changes | done (PR #35) |
| 4.1b | Phoneme-accurate lip-sync via Piper phoneme timestamps | follow-up PR (substantial — sidecar piper engine changes + frontend viseme overlay) |
| 4.2 | Eye-tracking / gaze direction | follow-up |
| 4.3 | Micro-expressions tied to mode (subtle smile, neutral attention) | follow-up |
| 4.4 | Better blink timing (silence-pocket-triggered while speaking) | done (PR #37) |

### 5. Voice texture

Cori is good but the same Cori for everything. Per-mode voice variants would let her sound closer/quieter in counsel, more clipped in advisor, slower/warmer in distress.

| ID | Item | Status |
|---|---|---|
| 5.1 | Audio post-processing (compression, EQ shifts) per mode | follow-up PR |
| 5.2 | Piper SSML-equivalent control (rate, pitch) per mode | follow-up PR |
| 5.3 | Alternate Piper voice models for different modes | follow-up |

### 6. Persona-prompt sharpening

The current persona is good but performance-y in places. Concrete prompt-level improvements:

| ID | Item | Status |
|---|---|---|
| 6.1 | Drop the literary-reference shopping list (Wilde/Feynman/Borges) — leans showy | this PR |
| 6.2 | Trim repetitive instruction blocks | this PR |
| 6.3 | Add 5–7 set-piece lines for specific moments (exact targets, not vibes) | this PR |
| 6.4 | More observation, less stylistic flourish | this PR |

---

## What this PR ships (Phase A)

Categories 1, 2, 3, 6 compose into a single coherent persona overhaul:
- Item 1.1, 1.2, 1.3, 1.4, 1.6 — personalization
- Item 2.1, 2.2, 2.3 — mode declaration system
- Items 3.1–3.4 — opinions + friction
- Items 6.1–6.4 — prompt sharpening

## What this PR defers (Phase B+)

- 1.5 recurring-thread surfacing (needs semantic memory — depends on `memory-lancedb` port)
- 2.4 auto-detected mode shifts (depends on crisis-floor merge)
- All of #4 embodiment phase 2 (substantial sidecar + frontend work, its own PR)
- All of #5 voice texture (separate PR per item — each is non-trivial)

## Change log

- 2026-06-01 — Document created. Phase A scope finalized.
- 2026-06-01 — Pivot: explicit mode-switching removed in response to user feedback ("why should we pick modes?"). Replaced with implicit "reads the room" via prompt criteria. Settings UI dropdown removed; vault key deprecated. Strengthened intelligence/warmth/kindness/no-limits sections in the prompt to push harder on those axes (real insight, actual warmth, present kindness, no false hedging).
