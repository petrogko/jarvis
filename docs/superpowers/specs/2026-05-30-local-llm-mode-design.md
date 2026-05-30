# Local-LLM Mode — Phase 1A Design

**Status:** design (for review)
**Date:** 2026-05-30
**Roadmap item:** Phase 1A in `docs/superpowers/roadmap/2026-05-30-aria-counsel-readiness.md` (closes S1, S13)
**Persona routing:** `software-architect` brainstormed the umbrella shape → `security-advisor` MUST review before any code lands (new egress mode + new sidecar surface + audit-log content discipline) → `code-reviewer` before commit → `test-runner` before "ready to merge".

---

## 1. Goal & threat model

Every Aria turn today round-trips to `api.anthropic.com`. For the counsel use case (genuinely sensitive personal material — relationship, health, finance, internal-state), three risks dominate:

1. **Egress visibility.** Anthropic sees the verbatim user turn + Aria's full system prompt + rolling summary + recalled memories. Even with no retention guarantees, the data leaves the Mac.
2. **Subpoena / breach surface.** Any third-party API enlarges the attackable surface beyond what `vault.py` + SQLCipher control.
3. **User mode-shift.** A user who knows everything is sent to a vendor talks differently than one who knows the conversation stays local. Self-censorship corrupts the counsel.

**Goal of 1A:** give the user a vault-controlled toggle to route LLM calls to a **local model running on the host (Ollama)**, and a per-conversation **seal** flag that forces local regardless of the global toggle. The default for non-sealed conversations stays `anthropic` — quality matters and counsel work is a subset.

**Non-goals.** Streaming. Multi-model routing logic (best-of). Local fine-tuning. Replacing Anthropic for action-dispatch reasoning (`[ACTION:X]` parsing stays where it is — only the chat generation backend is swappable).

## 2. Modes

New vault key `LLM_MODE` ∈ {`anthropic`, `local`, `hybrid`}. Default `anthropic`.

| Mode | Behavior |
|---|---|
| `anthropic` | All non-sealed turns go to Anthropic. Sealed conversations still force local (§5). |
| `local` | All turns go to local Ollama. Hard-fail if Ollama unreachable; no silent Anthropic fallback (§7). |
| `hybrid` | Sealed conversations → local (hard-fail). Non-sealed → Anthropic with silent fallback to local on Anthropic error. Fallback is **logged** in the audit log so the user can see it happened. |

`LLM_MODE` is added to the `/api/settings/keys` allowlist in `server.py:2800`. The UI surfaces it as a radio in the existing settings panel. Value changes take effect on the **next** turn; in-flight turns complete on the original backend.

## 3. Local-LLM transport — sidecar proxy

**Decision: route through the host sidecar.** The JARVIS container does NOT talk to Ollama directly.

Justification:
- The container→host story is already settled for sidecar (`host.docker.internal:9999`, shared-token, loopback bind, constant-time compare). Opening a second container→host port for `:11434` re-litigates that and doubles the attack surface to scan/firewall.
- `sidecar_client.py` already has the error-tolerant `httpx` + token pattern; adding `llm_chat_via_sidecar` is ~30 LOC.
- Auth, audit, and rate-cap live in one place (the sidecar) instead of being duplicated against a vendored Ollama client.
- The sidecar can multiplex: Ollama today, llama.cpp tomorrow, MLX next quarter. The JARVIS-side API stays stable.

Trade-off accepted: one extra hop on localhost adds ~1-3 ms latency. Negligible relative to model generation (seconds).

## 4. New sidecar endpoint — `POST /llm/chat`

Added to `host-sidecar/jarvis_sidecar/app.py` next to `/tts` and `/stt`. Same `X-SIDECAR-Token` auth via `require_token`.

**Request (JSON):**
```json
{
  "model": "llama3.3:70b-instruct-q4_K_M",
  "system": "<string, <=64 KiB>",
  "messages": [{"role": "user|assistant", "content": "<string>"}, ...],
  "max_tokens": 250,
  "temperature": 0.7
}
```

**Response (JSON, non-streaming):**
```json
{
  "text": "<assistant reply>",
  "model": "llama3.3:70b-instruct-q4_K_M",
  "input_tokens": 1842,
  "output_tokens": 87,
  "latency_ms": 4210
}
```

**Error shape** mirrors `/tts`: 400 on payload validation (size cap, empty messages), 413 on size, 429 on rate cap, 502 on Ollama unreachable, 504 on Ollama timeout.

**Implementation.** New `jarvis_sidecar/llm.py` calls Ollama's `POST http://127.0.0.1:11434/api/chat` with `"stream": false`. Streaming is explicitly deferred to v2 to keep the sidecar simple; the JARVIS voice path already buffers the full Haiku response before TTS, so non-streaming changes nothing user-visible.

**Caps (enforced server-side, sidecar):**
- System+messages JSON ≤ **64 KiB** total (mirrors `/spawn` prompt cap).
- Response ≤ **16 KiB** (Aria turns are short; 16 KiB is ~12 turns' worth of slack).
- Rate cap: **30 calls/minute per token**, sliding window. 429 on exceed. Counsel sessions are conversational, not batch — 30/min is plenty and bounds runaway loops.

## 5. Per-conversation seal

PR #25's `conversations` table gets a new column:

```sql
ALTER TABLE conversations ADD COLUMN sealed INTEGER NOT NULL DEFAULT 0;
```

**Semantics.** `sealed=1` means: this conversation's content has been declared counsel-private. The router (§10) forces local for every turn in this conversation, ignoring `LLM_MODE`. Sealing is **one-way**: a sealed conversation cannot be unsealed (preventing accidental re-routing of historical context to Anthropic). Starting a new conversation is the unseal path.

**UX for sealing.** Two paths, both shipped:
1. **Voice command:** "Aria, seal this." — handled by a new `[ACTION:SEAL_CONVERSATION]` tag emitted when the user input matches `re.compile(r"\b(seal|lock|counsel mode|private mode)\b", re.I)` and a confirmation utterance. Detection lives in `server.py:extract_action`.
2. **UI toggle:** a padlock icon on the active conversation card. Click → confirm modal → set `sealed=1`.

**Auto-seal (deferred).** Keyword-trigger auto-seal (e.g., user says "between us") is tempting but failure-modes are bad (silent routing change the user didn't ask for). Defer to Phase 2 once Aria has mode-declaration (P3).

**Visual indicator.** When the active conversation is sealed, the orb gets a subtle padlock overlay and the transcript header shows "Sealed — local model". Non-negotiable: the user MUST always know which backend served the current turn.

## 6. Model selection

The user installs Ollama themselves (it's a host app, not a container concern). `setup.sh` gains an opt-in flag `--with-ollama` that:

1. Checks `ollama --version` and prints install instructions if missing.
2. Pulls a default model the user picks from a short menu.
3. Writes the chosen model name to `LLM_LOCAL_MODEL` in the vault.

**Recommended defaults (M-series Mac):**

| Tier | Model | Memory | Why |
|---|---|---|---|
| Default (M3/M4 Max ≥ 64 GB) | `llama3.3:70b-instruct-q4_K_M` | ~42 GB | Best counsel quality at acceptable latency. |
| Lighter (32 GB or speed-prioritized) | `qwen2.5:14b-instruct-q5_K_M` | ~10 GB | Strong instruction-following; <2s first token. |
| Privacy-floor (no GPU / 16 GB) | `llama3.1:8b-instruct-q4_K_M` | ~5 GB | Counsel quality degraded but still usable. |

Model choice is per-install, surfaced as `LLM_LOCAL_MODEL` in vault settings UI. Switching models is a settings change, not a re-install.

## 7. Fallback semantics

| Situation | Behavior |
|---|---|
| `LLM_MODE=anthropic`, Anthropic down, non-sealed | Existing error path. Aria says "Apologies, sir...". No automatic local fallback (user didn't opt in). |
| `LLM_MODE=local`, Ollama down, sealed or non-sealed | **Hard-fail.** Aria says "Local model unreachable, sir. Conversation halted." No fallback to Anthropic — the user explicitly chose local. |
| `LLM_MODE=hybrid`, Anthropic down, non-sealed | Silent fallback to local. Audit-log entry records `fallback=true`. UI badge shows "answered locally". |
| `LLM_MODE=hybrid`, Ollama down, sealed | **Hard-fail.** Sealed never reaches Anthropic regardless of mode. |
| `LLM_MODE=hybrid`, both down | Standard error response. |

Sealed conversations NEVER fall back to Anthropic. This is the load-bearing invariant.

## 8. System-prompt portability

Aria's persona prompt (~250 lines) is tuned against Claude — specifically around `[ACTION:X]` tag emission and the British-butler register. Risks on Llama/Qwen:

- Action-tag fidelity. Llama 3.3 70B emits the tags correctly in informal testing; 8B and 14B models drop tags ~10–20% of the time.
- Register drift. Qwen tends to break role mid-conversation under emotional load.

**v1 decision.** Send the same prompt unchanged. Document the degradation in the UI ("Local models may be less consistent with persona") and accept it for v1.

**Follow-up (Phase 2).** Introduce a `persona_profile` abstraction: the persona prompt is templated per backend (`anthropic`, `local-llama`, `local-qwen`). Out of scope for 1A.

## 9. Security guards (security-advisor checklist)

1. **Prompt size cap** — 64 KiB total payload, enforced in the sidecar before the Ollama hop. 413 on exceed.
2. **Response size cap** — 16 KiB. Sidecar truncates and returns `truncated=true`.
3. **Per-token rate cap** — 30 calls/minute sliding window. 429 on exceed.
4. **System-prompt extractability** — accepted limitation. Local models are MORE prone to echoing the system prompt than Claude. Documented in `SECURITY.md` follow-up. NOT a release-blocker — the system prompt isn't a secret (it's in git), it's persona text.
5. **Audit log discipline (canary).** `audit_log` gets a new source `llm-chat` recording:
   ```json
   {"ts":"...","source":"llm-chat","backend":"local|anthropic",
    "model":"...","sealed":true,"input_tokens":N,"output_tokens":N,
    "latency_ms":N,"fallback":false}
   ```
   The audit log MUST NOT contain message content, system prompt, or any prefix thereof. Enforced by a unit test that runs a known-string-canary prompt and asserts the canary string never appears in `data/audit.jsonl` (§11).
6. **Sidecar token reuse** — same `X-SIDECAR-Token`. No new secret to manage.
7. **Loopback bind** — Ollama on host listens on `127.0.0.1:11434` only. Sidecar verifies at startup; logs a warning and refuses to proxy if Ollama is bound 0.0.0.0.
8. **DEVNULL discipline** — the sidecar's Ollama subprocess (if it ever spawns one; v1 assumes user-managed) follows the same stdin/stdout/stderr posture as `/spawn`.

## 10. JARVIS-side wiring — `llm_router.py`

New module. Single public function:

```python
async def chat(
    system: str,
    messages: list[dict],
    *,
    conversation_id: str | None,
    max_tokens: int = 250,
) -> ChatResult:
    """Returns ChatResult(text, backend, model, fallback_occurred)."""
```

Decision tree (in order):

1. Read `conversations.sealed` for `conversation_id`. If `sealed=1` → local. If Ollama unreachable → raise `LocalUnreachable` (caller renders hard-fail message).
2. Else read `LLM_MODE` from vault.
   - `anthropic` → Anthropic call. On error, raise.
   - `local` → local call. On error, raise.
   - `hybrid` → Anthropic call. On error, local call. Mark `fallback_occurred=True`.
3. Emit audit-log entry (no content, §9.5).
4. Return.

`generate_response` (server.py:1280) is refactored to:
- Build `system` + `messages` exactly as today.
- Pass them to `llm_router.chat(...)` instead of calling `anthropic_client.messages.create` directly.
- Wrap the existing error string ("Apologies, sir…") for non-sealed failures; emit "Local model unreachable, sir." for sealed/local hard-fails.

The other six `anthropic_client.messages.create` call sites (rolling summary, memory extraction, etc.) stay on Anthropic for v1. They operate on already-persisted material; routing them is a separate decision and adds complexity 1A doesn't need. Document this clearly in the spec footer and in `llm_router.py` docstring: **only the user-turn generation is routed in v1.**

## 11. Tests (hermetic)

- `tests/test_llm_router.py`:
  - `LLM_MODE=anthropic` + unsealed → Anthropic path called, local not called.
  - `LLM_MODE=anthropic` + sealed → local path called, Anthropic not called.
  - `LLM_MODE=local` + Ollama 502 → `LocalUnreachable` raised, no Anthropic fallback even when key is set.
  - `LLM_MODE=hybrid` + Anthropic raises + Ollama ok → local result returned with `fallback_occurred=True`.
  - `LLM_MODE=hybrid` + sealed + Ollama down → hard-fail (no Anthropic).
- `tests/test_sidecar_llm.py` (sidecar):
  - 200 happy path against mocked Ollama.
  - 413 on >64 KiB payload.
  - 429 after 31 calls in a minute.
  - 502 when mocked Ollama errors.
  - 401 without `X-SIDECAR-Token`.
- `tests/test_audit_log_no_prompt.py`:
  - Send a turn with canary string `"CANARY_XYZ_DO_NOT_LOG_ME_42"`.
  - Assert canary appears nowhere in `data/audit.jsonl`.
- `tests/test_conversation_seal.py`:
  - `sealed=1` is set-once; UPDATE that attempts to clear it raises.
  - New conversation defaults `sealed=0`.

All tests run without network: Ollama and Anthropic clients are injected as fakes.

## 12. Open questions for security-advisor

1. Should `LLM_MODE=local` be the default once the user enables Ollama, or stay opt-in per mode toggle?
2. Sealing is one-way per §5. Is there a recovery story for "I sealed by mistake" beyond "start a new conversation"? Tension between user-error UX and the guarantee that sealed content never reaches Anthropic.
3. Hybrid silent-fallback: should we require an additional vault flag (`HYBRID_FALLBACK_OK=true`) before silent fallback is enabled, or is the audit-log entry + UI badge sufficient consent?
4. Should the sidecar refuse to start at all if Ollama is bound on a non-loopback address, or just refuse to proxy (current proposal)?
5. Per-token rate cap is 30/min. Is that the right number against a single human user, or should we lower it (e.g., 10/min) to bound runaway agent loops more tightly?
6. The six non-user-turn Anthropic call sites (summary, memory extraction, etc.) are NOT routed in v1. Is that acceptable for the counsel threat model, or does S1 require routing all six before counsel-readiness is claimed?
7. Should sealed-conversation telemetry (count, duration) be emitted to the audit log at all? Even metadata-only might be too much.

---

## Security-advisor review applied (2026-05-30) — GO-WITH-FIXES

### Required fixes (must apply during implementation)
1. **Structurally enforce sealed-never-falls-back** — current spec relies on the router. Add a defense-in-depth `assert not conversation.sealed` inside the Anthropic call path immediately before `anthropic_client.messages.create`, so a future router bug cannot leak sealed content.
2. **DB-layer set-once for `sealed`** — add a SQLite `BEFORE UPDATE` trigger: `WHEN OLD.sealed = 1 AND NEW.sealed = 0 RAISE(ABORT)`. App-level check alone is bypassable.
3. **Hybrid silent-fallback must notify in-band** — Aria prepends a one-clause acknowledgment on the first fallback turn per session ("Anthropic unreachable; answering locally, sir."). Audit log + UI badge alone insufficient for a voice-first product.
4. **Ollama loopback enforced at sidecar startup, not per-request** — sidecar refuses to start if Ollama is bound non-loopback; fail-closed.
5. **Audit-log canary tests strengthened** — single fixed canary is theater. Tests must (a) embed canary at start, middle, end of user content; (b) embed canary in system prompt; (c) trigger fallback and re-check.
6. **Transitivity gap on unrouted call sites (§10 footer)** — rolling-summary and memory-extraction touch material from sealed conversations. For v1, either route those through local for sealed-source content OR refuse to summarize/extract from sealed conversations. **NOT acceptable as written.**

### Recommended
- Per-token max-in-flight cap (e.g., 2) alongside the 30/min rate cap.
- Surface `truncated=true` to JARVIS, don't swallow silently.
- Add action-tag-fidelity test against the chosen local model (CI-skip-by-default).
- Sidecar startup `curl 127.0.0.1:11434/api/tags` once and log model availability.
- Strip `[ACTION:X]` parsing entirely from sealed-conversation responses — counsel turns shouldn't dispatch browser/build actions; shrinks injection-to-action surface.

### Advisor's answers to §12 open questions
1. **Default mode when Ollama enabled:** stay opt-in (`anthropic` default).
2. **Sealed-by-mistake recovery:** no recovery; the guarantee is the product.
3. **`HYBRID_FALLBACK_OK` flag:** YES, require it.
4. **Sidecar startup on non-loopback Ollama:** refuse to start (Required #4).
5. **Rate cap:** lower 30/min → **10/min/token**.
6. **Unrouted Anthropic call sites:** NOT acceptable for sealed. See Required #6.
7. **Sealed-conversation telemetry:** metadata-only acceptable; conversation_id must be hashed or omitted.

### Drift
- SECURITY.md: new egress destination (Ollama on host), new vault keys, sealed invariant, system-prompt-echo limitation.
- ARCHITECTURE.md: new `llm_router.py`, new sidecar route, new trust-boundary line (container → sidecar → Ollama loopback).
- Membrane: none directly; `llm_router.py` becomes new high-trust module — add to CLAUDE.md persona routing as security-advisor-gated.
