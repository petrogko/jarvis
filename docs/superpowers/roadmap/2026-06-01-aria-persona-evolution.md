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
| 4.3 | Micro-expressions tied to register ([REG:soft|counsel|dry|playful|neutral] markers) | done (PR #40) |
| 4.4 | Better blink timing (silence-pocket-triggered while speaking) | done (PR #37) |

### 5. Voice texture

Cori is good but the same Cori for everything. Per-mode voice variants would let her sound closer/quieter in counsel, more clipped in advisor, slower/warmer in distress.

| ID | Item | Status |
|---|---|---|
| 5.1 | Baseline warmth chain (highShelf + compressor) | done (PR #36) |
| 5.2 | Per-register EQ + compression presets (browser-side, smooth ramps) | done (PR #41) |
| 5.3 | Piper SSML-equivalent control (rate, pitch) per mode | follow-up PR |
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

---

## Phase B — Digital Partner (the bigger goal)

User feedback: "A digital partner is a larger persona than just assistant. I want her an expert in all fields — business, risk, law, partnerships, finance, being a father." The gap between "assistant" and "partner" isn't model capability (Opus already has expert generalist knowledge). The gap is **what she persistently knows about HIM** — his stakes, his family, his deals, his patterns. Five capabilities in order of leverage:

### B.1 Persistent user profile ← **highest leverage, smallest scope**
A markdown file she maintains at `data/aria_profile.md` (encrypted via vault — PII). She reads it every turn; she updates it via a `[PROFILE_UPDATE: ...]` tag the same way she emits `[REG:X]`. Server-side merger applies updates. The profile holds: who he is, family (kids' names, ages, partner), active stakes (deals, decisions), open worries, values, patterns, recent threads. Foundation for everything that follows.
- **Status:** not started. ~1 PR, ~150 LOC.

### B.2 Semantic memory across all conversations
lancedb + embeddings (OpenClaw memory-lancedb port). Every conversation turn is embedded. Aria can recall "the conversation three weeks ago where you were debating the term sheet" when relevant. Unblocks "you mentioned this before — has it changed?" and recurring-thread surfacing (1.5 from Phase A).
- **Status:** not started. Multi-day. Depends on OpenClaw port.

### B.3 Document ingestion
Upload contracts, term sheets, P&Ls, school reports. She stores them encrypted and reads them in context when referenced.
- **Status:** Phase 1 done — text/markdown documents via API (`POST /api/documents` title+content), stored in encrypted memory DB. Title-based reference detection: when his turn contains a stored document's title (case-insensitive substring, min 4 chars), the full doc is injected into her prompt. 50 KiB per-doc cap. CRUD endpoints: POST/GET/DELETE `/api/documents`, GET `/api/documents/{id}`.
- **Deferred (phase 2):** PDF extraction (sidecar pdftotext / pymupdf with GPL discipline like Piper), frontend upload UI (modal + drag-drop), FTS-based retrieval for unnamed-doc queries, per-doc metadata (counterparty, draft/signed, source, tags).

### B.4 Domain priors
Small markdown briefs per domain at `aria_domains/<name>.md`. NOT general knowledge (Opus has it) — but the frameworks HE cares about, the red flags HE wants flagged, the questions HE wants her to ask. Loaded into context when a relevant turn fires (keyword classifier in `aria_domains.py` matches → up to 3 briefs concatenated and injected into system prompt).
- **Status:** ✅ DONE. Six domains shipped: business / legal / fatherhood / finance / risk / partnerships. Adding more is trivial — drop a markdown file in `aria_domains/`, add a keyword regex to `_DOMAIN_KEYWORDS`. Each brief is a scaffold meant to drift toward what HE actually wants over time.

### B.5 Proactive turns
She notices things between conversations and brings them up on next connect. "You mentioned the partnership three weeks ago and went quiet — where are you with it?" Right now she's reactive. A partner notices.
- **Status:** Phase 1 done — `_identify_open_thread()` fires on every WS reconnect with a resumed conversation. Haiku call over profile + last 3 conversation tails returns one open thread or "none"; if a thread is found, the opener brief includes it. Costs one Haiku call per reconnect (cheap). Phase 2 (background scheduler that surfaces threads even without a reconnect — desktop notification, scheduled check-ins) is queued.

### Dependency graph

```
B.1 (profile) ──┬──> B.4 (domain priors plug into profile)
                ├──> B.5 (proactive — needs open threads)
                └──> (everything benefits from B.1)

B.2 (semantic memory) ──┬──> B.3 (doc ingestion stores here)
                        └──> B.5 (proactive needs recall)
```

### Recommended ship order

1. **B.1** — biggest "she knows me" jump per LOC. Foundation.
2. **B.4** — start with one domain, see how it lands, expand. Composes with B.1 immediately.
3. **B.2** — structural commitment but unblocks B.3 and B.5.
4. **B.3** — once B.2 is in, doc ingestion is small.
5. **B.5** — last, after she has the data to actually surface anything meaningful.

---

## Change log

- 2026-06-01 — Document created. Phase A scope finalized.
- 2026-06-01 — Pivot: explicit mode-switching removed in response to user feedback ("why should we pick modes?"). Replaced with implicit "reads the room" via prompt criteria. Settings UI dropdown removed; vault key deprecated. Strengthened intelligence/warmth/kindness/no-limits sections in the prompt to push harder on those axes (real insight, actual warmth, present kindness, no false hedging).
- 2026-06-01 — Phase B added: "Digital Partner" capabilities (B.1 persistent profile, B.2 semantic memory, B.3 doc ingestion, B.4 domain priors, B.5 proactive turns). Triggered by user feedback: "I want her an expert in all fields. A digital partner is a larger persona than just assistant." Phase A handled tone/voice/avatar surface; Phase B handles knowing-him persistently.
