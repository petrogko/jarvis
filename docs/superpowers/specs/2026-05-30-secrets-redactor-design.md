# Secrets-Detection Redactor — Design

**Status:** design (for review)
**Date:** 2026-05-30
**Roadmap item:** Phase 1D of `docs/superpowers/roadmap/2026-05-30-aria-counsel-readiness.md`
**Persona routing:** `security-advisor` MUST review (new trust-boundary filter on a privacy-critical path) → `software-architect` validates wiring across the WS handler + persistence layer → `code-reviewer` before commit → `test-runner` before any "ready to merge" claim.

---

## 1. Goals

1. **Prevent obvious secrets from being persisted** to the memory store (`memory.py`, vault-backed SQLite) when the user speaks them aloud.
2. **Prevent the same secrets from being sent to Anthropic** in the `generate_response` call — the second leak vector, equally important.
3. **Counsel-grade defense in depth.** The user *should* know not to read their SSN to a microphone; Aria assumes they will anyway.
4. **Symmetric redaction.** The same redacted string flows into memory AND into the LLM prompt, so memory reflects what the model actually saw. No "the LLM remembers X but the DB stored Y" drift.
5. **Pure-regex, sub-10ms.** No LLM call inside the redactor. The voice loop budget will not absorb another model hop.

## 2. Non-goals

- A DLP framework. No ML classifier, no entropy-tuned scanner, no per-org policy engine.
- Redacting common PII that has legitimate counsel-context value (names, employer, address). Aria's whole point is to know the user's life; over-redaction breaks the product.
- Redacting Aria's *outputs* to the user's screen/voice — only her *persisted* and *re-sent* text. The user already knows what they just said.
- Coverage of every conceivable secret format. We ship the eight categories in §3 and add more on demand.

## 3. Detection categories

Concrete patterns in `secrets_redactor.py`. Each detection returns `Detection(category, span, redacted_token)` — never the matched bytes outside the function.

| Category | Pattern | Validator | Default |
|---|---|---|---|
| `ssn` | `\b\d{3}-?\d{2}-?\d{4}\b` | reject `000-*`, `666-*`, `9xx-*`; reject if all digits identical | on |
| `card` | `\b(?:\d[ -]?){13,19}\b` | Luhn checksum; reject 4-digit-year-only matches; reject if surrounded by `19\d\d` / `20\d\d` context | on |
| `routing` | `\b\d{9}\b` | ABA mod-10 checksum (`3*(d1+d4+d7) + 7*(d2+d5+d8) + (d3+d6+d9) % 10 == 0`); skip if the surrounding token is a phone area code | on |
| `api_key` | literal prefixes: `sk-`, `sk-ant-`, `tvly-`, `ghp_`, `github_pat_`, `gho_`, `xoxb-`, `xoxp-`, `pk_live_`, `pk_test_`, `AKIA[0-9A-Z]{16}` | length sanity (≥ 20 chars after prefix for the `*_` shapes) | on |
| `hex_secret` | `\b[a-f0-9]{32,64}\b` | Shannon entropy ≥ 3.5 bits/char; skip git SHAs in commit-message contexts (heuristic: preceded by `commit `/`sha `) | on |
| `spoken_password` | `\b(?:my password (?:is|:)|password (?:is|:|colon))\s+(\S+(?:\s\S+){0,3})` (case-insensitive) | captures up to 4 tokens after the trigger | on |
| `phone` | `\b(?:\+?1[-\s]?)?\(?\d{3}\)?[-\s]?\d{3}[-\s]?\d{4}\b` | — | **off** (high FP, low harm) |
| `email_plus_name` | — | — | **off** (legit counsel data) |

Categories are toggled at module level via `SECRETS_CATEGORIES` (vault key, comma-separated; defaults shown above).

## 4. Redaction format

Replace each match with `[REDACTED:<category>]` — e.g., `[REDACTED:ssn]`, `[REDACTED:card]`, `[REDACTED:api_key]`. Keeping the category visible lets the LLM stay coherent: "I redacted what looked like an SSN" is a fine thing for Aria to reason about; a generic `[REDACTED]` strips that signal.

Adjacent detections are merged into one token when they overlap; otherwise each is replaced independently. The redactor returns the rewritten string and the detection list.

## 5. Two-mode operation

Vault key: `SECRETS_MODE` ∈ {`off`, `warn`, `strict`}. Default **`warn`**.

- **`off`** — scan still runs (cheap), audit-logs nothing. Pass-through. Intended for development; not recommended.
- **`warn`** (default) — scan, audit-log every detection, **but do NOT modify the text**. Aria adds a one-line nudge to her reply: *"That sounded like an account number, sir — shall I keep it out of the record?"* If the user says yes (`[ACTION:SECRETS_MODE strict]` or "redact that"), the redacted form replaces the live history entry AND is re-stored in memory (`memory.py` update). The original is not persisted to the durable store between the warn and the confirmation — we hold it only in the in-process `history` list until the user decides.
- **`strict`** — scan, redact silently before persist + LLM call. Audit-log every detection with `redacted=true`. Aria gets the redacted text only; she never sees the raw secret.

Voice-toggleable via `[ACTION:SECRETS_MODE strict]` / `[ACTION:SECRETS_MODE warn]`. The action handler lives next to the other `[ACTION:*]` dispatchers in `server.py` and writes the vault key through `_vault_mod.session().settings.set`.

## 6. Module API & wiring

New file `secrets_redactor.py`:

```python
def scan(text: str, *, categories: Iterable[str] | None = None) -> list[Detection]: ...
def redact(text: str, *, categories: Iterable[str] | None = None) -> tuple[str, list[Detection]]: ...
```

`Detection` is a dataclass: `category: str`, `start: int`, `end: int`, `replacement: str`. The matched bytes live ONLY inside the function; no field carries them to callers.

Call sites in `server.py`:

1. **WS handler — user turn.** After `user_text` is assembled from the STT/Web Speech transcript and BEFORE either the LLM call or the `history.append({"role": "user", ...})` at line ~2714. The redacted string replaces `user_text` for both downstream uses.
2. **WS handler — assistant turn.** After `response_text` returns from `generate_response` (lines ~2409 / ~2513) and BEFORE the `history.append({"role": "assistant", ...})` at line ~2715. Catches the case where the model echoes a secret back (the user said the SSN in turn N-1; the model repeats it in turn N).
3. **Memory pipeline.** The background `memory.py` extractor receives the already-redacted strings via the in-memory `history` — no second hook needed, as long as the extractor reads from `history` (it does).
4. **Resume seeding.** `[ACTION:SECRETS_MODE]` dispatcher updates the vault key. The conversation-history reload path on WS reconnect already pulls from the persisted (redacted-on-write) store, so no extra filter there — but see §11.

The redactor is invoked from the main WS coroutine; it is `def`, not `async def`. At < 10ms it does not need to be scheduled off-thread.

## 7. Performance

All eight detectors compile to a small set of pre-compiled `re.Pattern` objects at import time. Worst-case input is a ~500-char STT transcript; the combined scan on a benchmark M-series should complete in < 10 ms (acceptance criterion, asserted in tests). No backtracking-prone alternations; the Luhn / ABA validators run only on the small number of digit-cluster candidates.

## 8. False-positive handling

- **Card vs. year.** A 4-digit run inside a longer digit sequence is allowed; a bare `1989` or `2024` is rejected (not 13–19 digits). The Luhn check eliminates most accidental 16-digit runs.
- **Routing vs. phone.** A 9-digit run preceded or followed by `-\d{4}` is treated as part of a phone number and skipped.
- **Hex vs. git SHA.** Tokens preceded by `commit ` / `sha ` / `git ` (case-insensitive, within 8 chars) are skipped. Acceptable residual FP rate: < 1 per 1000 conversational turns measured across a 30-day sample. Documented; if exceeded, the category gets a sharper validator, not removal.

## 9. Vault key & audit log

- New vault keys: `SECRETS_MODE`, `SECRETS_CATEGORIES`. Both added to the `allowed` set in `api_settings_keys` at `server.py:2800`.
- UI surfaces both in `frontend/src/settings.ts` (separate `code-reviewer`-tagged follow-up; not in this spec's scope).
- Audit log entry per detection (via `audit_log`):
  ```
  verb=secret_detected
  category=<ssn|card|routing|api_key|hex_secret|spoken_password|phone>
  redacted=<true|false>
  mode=<warn|strict|off>
  conversation_id=<uuid>
  turn_role=<user|assistant>
  ```
  **NEVER** log the matched bytes, the length of the match, the offset, or any prefix/suffix. The reviewer should be able to count detections, not reconstruct any.

## 10. Aria persona integration

Persona file (`.claude/agents/aria.md` or its successor) gets a short addendum:

> When `SECRETS_MODE=warn` and the redactor reports a detection on a user turn, append exactly one sentence to your reply asking whether to redact, in your voice — e.g., "That sounded like an account number, sir; shall I keep it out of the record?" Do not repeat the suspected value back. When `SECRETS_MODE=strict`, do not mention the redaction unless the user asks; the `[REDACTED:*]` token is your only signal.

This is one addendum block, not a persona rewrite.

## 11. Tests

Hermetic, under `tests/test_secrets_redactor.py`:

- `test_ssn_positive_dashed`, `test_ssn_positive_undashed`, `test_ssn_negative_year_only`, `test_ssn_negative_repeated_digits`
- `test_card_positive_visa_luhn`, `test_card_negative_year`, `test_card_negative_luhn_fail`
- `test_routing_positive_aba`, `test_routing_negative_phone_neighbor`
- `test_api_key_positive_each_prefix` (parametrized), `test_api_key_negative_short`
- `test_hex_secret_positive`, `test_hex_secret_negative_git_sha_context`
- `test_spoken_password_positive_is_form`, `test_spoken_password_positive_colon_form`, `test_spoken_password_negative_unrelated`
- `test_phone_off_by_default`
- `test_redact_output_exact_token` — asserts `[REDACTED:ssn]` literal
- `test_warn_mode_passes_text_through_with_detections`
- `test_strict_mode_replaces_in_history_and_llm_call` (integration with a stubbed `generate_response`)
- `test_audit_log_never_contains_matched_bytes` — patches `audit_log.write`, asserts none of the matched secrets appear in any logged field
- `test_perf_under_10ms_on_500_char_input` — `assert elapsed_ms < 10`

## 12. Open questions (for `security-advisor`)

1. **Resume seeding.** On WS reconnect the handler reloads recent turns into the in-memory `history`. The store already holds the redacted form (correct). But: should we *also* run the redactor over the loaded text in case an earlier version of Aria wrote raw secrets pre-1D? One-shot migration vs. permanent post-load filter — pick.
2. **Redaction trail UI.** Should the settings panel surface a count of recent detections (helpful for the user to know the filter is working) without exposing the bytes (we must not)? Counter-only is probably fine; want sign-off.
3. **`[REDACTED:*]` token leakage.** On resume, do we strip `[REDACTED:*]` tokens from the model's input entirely, replacing with a neutral filler ("[earlier turn omitted]") so the model can't reason "the user has had 4 SSNs redacted, they probably bank at X"? Inferential-information question; counsel-grade may demand it.
4. **STRICT-mode confirmation UX.** When STRICT is on, should Aria *ever* surface "I redacted something" — for example after the conversation ends — or is the audit log the only acknowledgment? Tradeoff: user trust vs. silence-by-design.

## 13. Out-of-scope follow-ups

- LLM-based secondary classifier for ambiguous cases (e.g., "my account number is one two three four..."  spoken as words, not digits).
- Per-conversation user-tunable category toggles (vault-global only in v1).
- Redacting Aria's spoken/displayed output to the user (out of scope — see §2).
- A "redaction trail" replay tool. Deferred until §12.2 is answered.

---

## Security-advisor review applied (2026-05-30) — GO-WITH-FIXES

### Required fixes (must apply during implementation)
1. **Ordering on assistant reply:** `extract_action` runs BEFORE redact, not after. Otherwise a model output containing both an action and a hex blob risks the regex eating action bytes.
2. **WARN-mode raw-secret exposure window unacceptable as written** — raw secret stays in `history` pending confirmation; the next turn ships it to Anthropic verbatim, and any crash/reconnect can flush it to persistence. Fix: redact-in-history immediately; hold raw only in a short-lived `pending_unredactions[conversation_id] = (raw, detection)` map with hard TTL (60 s) and single-use semantics. "Hold raw in history" is the threat path, not the safe path.
3. **`[REDACTED:category]` inferential leak on resume** — current turn keeps the category (Aria needs context to reason); on resume (`load_recent_messages`), collapse all `[REDACTED:*]` to neutral `[REDACTED]` and group consecutive ones into `[earlier sensitive content omitted]`. Four-SSN aggregation is a real counsel-grade concern.
4. **Audit-log invariant extends to exception paths** — wrap the redactor in `try/except`; log `verb=secret_detector_error category=<cat> exc_type=<type>` with NO message and NO traceback. A stack trace from a buggy regex can carry the input string. Add `test_audit_log_never_contains_matched_bytes_on_exception`.
5. **Spoken-password regex too narrow** — missing "the password is X", "use X as the password/passcode/PIN", "type X" / "enter X", "passcode is", "PIN colon". Extend trigger alternation: `(?:my |the |our )?(?:password|passcode|passphrase|pin)\s+(?:is|:|colon)` plus `(?:use|type|enter)\s+(\S+)\s+(?:as\s+(?:the\s+)?(?:password|passcode|pin))`.

### Recommended — additional categories
JWT (`eyJ...eyJ...`), PEM blocks (`-----BEGIN [A-Z ]+-----`), OpenSSH private keys (`-----BEGIN OPENSSH PRIVATE KEY-----`), spoken `ssh-rsa` keys, bcrypt (`\$2[aby]\$...`), IBAN (mod-97 validator), Stripe modern keys (`rk_live_`, `whsec_`), Slack (`xoxe-`). PEM/JWT are highest-value for counsel-mode.

### Recommended — engineering
- Pre-compile + cache patterns as module-level `_PATTERNS: dict[str, re.Pattern]`.
- Performance acceptance criterion: `p95` over 1000 iterations (not single-shot — too noisy on M-series). Plus 4-KiB adversarial input (`"9" * 4096`) to prove card regex doesn't catastrophic-backtrack.
- Hard input-length cap (4 KiB) + per-call wall-clock guard on the assistant-reply path.

### Advisor's answers to spec's open questions
1. **§12.1 Resume redaction:** Both. One-shot migration over existing store (idempotent, gated on `SECRETS_MIGRATION_DONE` vault flag) AND permanent post-load filter as belt-and-braces.
2. **§12.2 Counter UI:** Counter-only is fine. Show aggregate counts per category over rolling 7-day window; never per-conversation, never timestamps fine enough to correlate with a specific turn.
3. **§12.3 `[REDACTED:*]` on resume:** YES, strip. See Required #3.
4. **§12.4 STRICT acknowledgment:** Audit log is durable record. Surface end-of-session toast only on user request via dedicated `[ACTION:SHOW_REDACTION_COUNT]`. Don't volunteer mid-conversation.

### Drift
- SECURITY.md: new "Data-handling: secrets redaction" subsection — categories, two modes, audit-log invariant, "what the LLM saw == what we persisted" invariant.
- ARCHITECTURE.md: new `secrets_redactor.py` module; redactor sits between STT/LLM-out and both persistence + LLM-in.
- Membrane: SECURITY.md tripwire will fire — expected.
