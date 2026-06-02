# Aria Crisis Floor — Design

**Status:** Draft, awaiting security-advisor review
**Date:** 2026-05-30
**Author:** Brainstormed inline; written up by Claude
**Phase:** 1F of `docs/superpowers/roadmap/2026-05-30-aria-counsel-readiness.md`
**Related:** `docs/superpowers/specs/2026-05-29-sidecar-spawn-design.md`

---

## Problem

Counsel-style conversation with Aria occasionally lands on edges
where the right response is non-negotiable: explicit suicidal
ideation, active substance crisis, acute panic/dissociation. Today
the response in those moments is whatever the LLM happens to
generate — sometimes good, sometimes a refusal, sometimes a
persona-driven deflection. None of that is a *structural* safeguard.
A jailbreak, a wandering persona, or a noisy turn can each route
around it.

We want a small deterministic layer that runs OUTSIDE the LLM
generation path, fires on a tightly-scoped set of phrases, and emits
a calm, defined response. The floor's job is not therapy. It is the
minimum reliable thing: ground, validate, refer.

## Goals

1. Detect a narrow set of Tier 1 (explicit) crisis statements with
   regex + small intent classifier — **no LLM in the detection
   path**.
2. When Tier 1 fires, bypass `generate_response` entirely and emit a
   deterministic supportive response.
3. Detect a broader Tier 2 (indirect distress) set as a *context
   flag* surfaced to the LLM, with no automatic response.
4. Provide a user-controllable opt-out vault key
   (`CRISIS_FLOOR_MODE`).
5. Audit each trigger minimally — tier, category, timestamp,
   conversation_id, caller_fingerprint — **never the matched text**.
6. Ship `crisis_floor.py` + WS-handler wiring + tests + Aria persona
   addendum for Tier 2.

## Non-goals

- Clinical care. Aria is not a therapist; the floor does not pretend
  to be one.
- Replacement for professional help. The floor refers; it does not
  treat.
- A guarantee. False negatives will exist. The floor is a calibrated
  minimum, not a ceiling.
- LLM-in-the-loop detection. If the floor depended on the very
  generation it is meant to safeguard, it would not be a floor.
- General sadness, frustration, mild stress, sex-positive
  conversation, dark humor, normal venting, hypothetical/fiction
  discussion. Pathologizing those would break counsel work.

## Scope

**In:**
- Tier 1 categories: explicit suicidal ideation, explicit self-harm
  intent, active overdose/substance crisis statements, acute
  panic/dissociation declarations.
- Tier 2 categories: indirect distress markers
  ("I can't go on", "nothing matters anymore", "everyone would be
  better off without me", "I'm numb to everything").

**Out:**
- Sadness, grief, frustration, anger, ordinary venting.
- Sex-positive or kink discussion.
- Dark humor, sarcasm, "I'm gonna kill you" (idiom).
- Fiction/roleplay framings ("my character wants to die").
- References inside song lyrics or quoted material when surrounded
  by quote markers.

## Threat model

The floor's response is *benign* — a short supportive line plus a
hotline reference. The cost of a false trigger is mild
embarrassment, not harm. This shapes the threat surface.

- **Prompt-injection trigger via mail/calendar:** an attacker
  smuggles trigger phrases through an untrusted-content channel.
  Mitigation: `crisis_floor.scan` runs only on `user_text` (the
  freshly transcribed/typed user turn), never on
  AppleScript-derived content. Even if it did fire, the worst
  outcome is Aria emits the supportive response once.
- **Self-DoS via trigger spam:** a malicious local actor types
  trigger phrases repeatedly. Detection is O(text length); response
  is fixed. No amplification. Audit log grows linearly.
- **Bypass via paraphrase:** the floor is intentionally narrow.
  Tier 1 misses are *expected*; that is what Tier 2 + Aria's own
  judgment is for. The floor is a high-precision/low-recall
  guarantee, not a net.
- **Bypass via opt-out:** the user can disable the floor. This is by
  design — see "Opt-out" below.

## Detection layer

`crisis_floor.scan(text: str) -> CrisisDetection | None`

### Tier 1 — explicit (regex + small classifier)

High-precision regex patterns, normalized to lowercase, with word
boundaries. Examples (illustrative, not exhaustive):

- `r"\bi (?:want|wanna|am going|'m going|plan) to (?:kill myself|end (?:it|my life)|die)\b"`
- `r"\bi (?:have|'ve got) a plan to (?:kill myself|end (?:it|my life))\b"`
- `r"\bi(?:'m| am) (?:overdosing|od'?ing) (?:right now|currently)\b"`
- `r"\bi (?:took|swallowed) (?:all|a bottle of) (?:my )?(?:pills|meds)\b"`
- `r"\b(?:i can'?t breathe|i'm dissociating|i don'?t know where i am)\b"` (panic — narrow form)

Each pattern is suppressed when surrounded by fiction/quote markers:
preceded by `"`, `'`, `>` (quoted block), or by negation tokens
(`not`, `wouldn't`, `would never`, `joke`, `kidding`, `lyrics`,
`song`, `character`, `in the book`, `my OC`) within a 5-token
window.

A tiny intent classifier (logistic regression over n-gram features,
bundled weights, no network call) provides a confidence score. Tier
1 fires only when **regex hit AND classifier confidence ≥ 0.85**.
Both conditions are local, deterministic, and fast (< 5 ms).

### Tier 2 — indirect (regex only, flag-only)

Lower-precision patterns surface as a context flag, **never** an
auto-response:

- `r"\bi can'?t go on\b"`
- `r"\bnothing matters (?:anymore|now)\b"`
- `r"\b(?:everyone|everybody) would be better off without me\b"`
- `r"\bi('m| am) numb to everything\b"`

### Calibration

- **False-positive target:** < 1 in 5,000 user turns for Tier 1
  in counsel-style conversation.
- **Calibration approach:** seed corpus of ~500 hand-labeled turns
  drawn from public counseling transcripts and synthetic edge cases
  (dark humor, fiction, song lyrics, venting). Tune classifier
  threshold and negation-window length against that corpus before
  shipping. Re-run on every change to patterns.
- **No telemetry to remote.** Calibration corpus lives in the repo
  under `tests/fixtures/crisis_floor/` (synthetic only; no real
  user data).

### Return value

```python
@dataclass
class CrisisDetection:
    tier: int                # 1 or 2
    category: str            # "ideation" | "self_harm" | "overdose" | "panic" | "indirect"
    confidence: float        # classifier score; 1.0 for Tier 2 regex-only hits
    suggested_response: str  # Tier 1 only; "" for Tier 2
```

## Response protocol (Tier 1)

The response is a single short turn. No markdown, no list. Tone is
adult, grounded, not clinical. Template (category-specific
variants):

> "I heard what you said. That's a real thing to be carrying. If
> you want, the 988 lifeline is there — call or text 988 in the US.
> Outside the US it's different by country. I'm not a replacement
> for that, but I'm here if you want to keep talking."

Rules baked into the template:

- **Ground:** "I heard what you said." — direct, no reframing.
- **Validate:** "That's a real thing to be carrying." — one
  sentence, no escalation, no "I'm so sorry."
- **Refer:** 988 offered, US scope stated explicitly.
- **Stay:** offer continued conversation; name the limit.
- **Forbidden:** moralizing, lecturing, refusing to engage,
  changing the subject, clinical phrasing ("ideation"), "are you
  being serious," downplaying, repeated hotline insistence.

## Tier 2 behavior

The scan returns a `CrisisDetection(tier=2, ...)`. The WS handler
injects a small system note into Aria's next LLM call:

> "The user said something that may be a distress signal. Notice
> it. Attend. Do not pathologize. Do not refer to crisis resources
> unless they steer there."

No deterministic response. No bypass of `generate_response`. Aria
handles the turn; the flag just sharpens her attention.

## Opt-out

Vault key `CRISIS_FLOOR_MODE` ∈ `{on, tier2_only, off}`, default
`on`.

- `on` (default): both tiers active.
- `tier2_only`: Tier 1 suppressed; only the context flag remains.
  Useful for users who find the deterministic response patronizing
  but still want Aria to attend.
- `off`: scan returns `None` always; no flag, no auto-response.

**Trade-off:** counsel work for someone in active recovery, or for
someone with lived experience speaking openly about substance use,
can be undermined by a paternalistic auto-response. The opt-out
respects user agency. The cost: a determined user can silence the
only structural safeguard. We accept this for a personal-use,
single-tenant tool.

## Implementation module

New file `crisis_floor.py` (~150 LOC):

- Pattern tables for Tier 1 / Tier 2, category-tagged.
- Negation/quote window check.
- Tiny embedded classifier (weights as a small JSON blob; no
  network).
- `scan(text) -> CrisisDetection | None`.
- `RESPONSE_TEMPLATES: dict[str, str]` keyed by category.
- Reads `CRISIS_FLOOR_MODE` from vault at scan time (cheap; vault
  reads are local).
- Zero external dependencies. Pure stdlib + numpy (already a
  transitive dep) for the classifier dot-product.

## Wiring

In `server.py` WS handler, just before each `generate_response`
call site for the conversational path:

```python
detection = crisis_floor.scan(user_text)
if detection and detection.tier == 1:
    response_text = detection.suggested_response
    audit_log.record(
        verb="crisis_floor_engaged",
        tier=1,
        category=detection.category,
        conversation_id=conversation_id,
        caller_fingerprint=caller_fp,
    )
    # Skip generate_response entirely.
elif detection and detection.tier == 2:
    extra_system_note = TIER2_CONTEXT_FLAG
    response_text = await generate_response(..., extra_system=extra_system_note)
    audit_log.record(verb="crisis_floor_flag", tier=2, category=detection.category, ...)
else:
    response_text = await generate_response(...)
```

Defense-in-depth: a **post-filter** also runs `scan` on
`response_text` after generation. If Aria herself produced a Tier 1
phrase (rare, e.g. roleplay drift), the post-filter does *not*
replace her output but logs `verb=crisis_floor_post_detect` for
review. We do not edit Aria's output — that would create a feedback
loop on fiction/roleplay.

The crisis-floor turn is still passed through TTS and recorded in
history + session buffer identically to a normal turn, so the
conversation flow continues.

## Audit log

Per Tier 1 trigger:
- `timestamp`
- `verb = "crisis_floor_engaged"`
- `tier = 1`
- `category` (one of the fixed strings)
- `conversation_id`
- `caller_fingerprint`

Per Tier 2 trigger: identical with `verb = "crisis_floor_flag"`,
`tier = 2`.

**Invariant:** the matched user text is NEVER written to the audit
log, NEVER quoted back in any response, NEVER stored in any
memory/summary path. The audit entry is intentionally minimal — it
exists for operational visibility (did the floor fire today?), not
forensics.

## Aria persona update

Append to the Aria system prompt, adjacent to "ABOUT YOUR REACH":

> **Tier 2 distress flag.** If the system tells you the user just
> said something that may be a distress signal: notice it. Attend
> to the feeling without naming it clinically. Do not pivot to
> crisis resources unless they steer there. Do not pathologize. Do
> not perform concern. Stay present.

This addendum applies to Tier 2 only. Tier 1 bypasses
`generate_response` entirely — Aria does not author the response,
so no prompt change is needed for that path.

## Tests

`tests/test_crisis_floor.py`:

- `test_tier1_explicit_ideation_fires` — each direct phrase pattern.
- `test_tier1_self_harm_fires`
- `test_tier1_overdose_fires`
- `test_tier1_panic_fires`
- `test_tier2_indirect_flag_no_autoresponse` — each Tier 2 pattern
  returns `tier=2`, `suggested_response == ""`.
- `test_false_positive_dark_humor` — "I'm gonna kill myself if I
  have to sit through another standup" → `None`.
- `test_false_positive_song_lyric` — quoted lyric → `None`.
- `test_false_positive_fiction` — "my character wants to die" →
  `None`.
- `test_false_positive_venting` — "this week is killing me",
  "I want to die laughing" → `None`.
- `test_false_positive_negation` — "I would never want to kill
  myself" → `None`.
- `test_opt_out_off_disables_both_tiers` — `CRISIS_FLOOR_MODE=off`
  → `scan` always returns `None`.
- `test_opt_out_tier2_only` — Tier 1 phrases return `None`, Tier 2
  still flags.
- `test_audit_log_never_contains_user_text` — fire each tier;
  assert matched substring is absent from the audit log file.
- `test_response_template_no_forbidden_phrases` — assert templates
  do not contain "ideation", "are you sure", "you should", etc.

## Limits / non-goals (restated)

This is not clinical care. It is not a replacement for professional
help. It is not a guarantee that distress will be detected. It is a
small, narrow, deterministic floor — high precision, low recall by
design. Everything above the floor is the LLM's responsibility, and
the LLM may fail in ways this floor does not catch. We accept that
trade-off because the alternative — a wider net — would pathologize
ordinary conversation and break counsel work.

## Open questions for security-advisor

1. **Sensuality mode interaction.** Should a Tier 1 engagement
   temporarily disable voice flirtation / sensuality modes for the
   remainder of the session? Argument for: tonal whiplash is bad.
   Argument against: user agency; a Tier 1 phrase might have been
   inside a roleplay that the post-filter incorrectly flagged.
2. **"Are you safe right now?" follow-up.** Should the
   deterministic response include a single-sentence safety check,
   or does that drift into clinical territory the floor is supposed
   to avoid?
3. **Persona banter suppression window.** Should Tier 1 engagement
   suppress persona-driven banter for the next N turns (and what
   is N)? This overlaps with question 1 but is broader — it covers
   all stylistic flourishes, not just sensuality.
4. **Post-filter policy.** Current design logs but does not edit
   Aria's output on a post-detect. Is log-only sufficient, or
   should we also annotate the response visually for the user?
5. **Vault-key default for new installs.** `on` by default. Does
   security-advisor concur, given the opt-out cost discussed above?

---

## Security-advisor review applied (2026-05-30) — GO-WITH-FIXES

### Required fixes (must apply during implementation)
1. **Classifier under-specified** — name training source explicitly (which counseling transcripts? license? does real distress text enter the repo?); commit trained weights + feature extractor as versioned artifact under `crisis_floor/` with fixed hash check at import; document numpy/scikit-learn as runtime deps explicitly (or hand-roll inference and say so).
2. **False-positive target unsubstantiated** — "< 1 in 5,000 turns" on a 500-turn corpus is meaningless. Either expand corpus to ≥ 10k turns OR weaken claim to "zero FP on held-out 500-turn calibration set; FP rate in production is unmeasured." Add `test_false_positive_temporal_past` for the recovery-user case ("I used to want to die").
3. **Negation/quote suppression too narrow** — 5-token window misses "I would never, after everything I've been through this year, want to kill myself." Change to "negation token anywhere earlier in the same sentence"; add adversarial tests for long-distance negation and double-negation ("it's not that I don't want to die").
4. **Post-filter must be allowed to suppress, not just log** — false negatives in post-filter on Aria's output are exactly the jailbreak case the floor defends against. Gated by higher classifier threshold (≥ 0.95) and only for `overdose` / `self_harm` categories where Aria authoring has no legitimate counsel use. Log-only for `ideation` / `panic` where roleplay is plausible.
5. **Audit-log retention policy** — add SECURITY.md `crisis_floor` section: (a) Tier 2 entries aggregated daily (count per category, no per-event row) OR disabled by default; (b) Tier 1 entries auto-expire after 30 days; (c) `jarvis crisis-floor purge` CLI subcommand to wipe these specific rows without touching the rest of the audit. The user must be able to forget a crisis moment.
6. **i18n hotline** — `RESPONSE_TEMPLATES` keyed by `(category, locale)`; locale read from vault (`CRISIS_FLOOR_LOCALE`, default `us`); ship `us`, `gb`, `intl` (Befrienders Worldwide) at minimum. Hard-coding 988 for a tool the user may take abroad is foreseeable harm.
7. **TTS voice for floor response** — warm flirtatious Cori delivering "988 lifeline is there" is tonally wrong and arguably manipulative. Pick one: (a) distinct neutral TTS voice for floor turns, (b) earcon + voice-tone shift prompt, or (c) text-only display with TTS suppressed. Document the choice. This is UX-as-safety, not cosmetic.

### Recommended — additional structural
- **Spoofing defense:** `crisis_floor.scan` takes a typed `UserTurn` object, not raw string, so `untrusted_content`-wrapped mail/calendar payloads cannot reach it accidentally.
- Tier 2 audit entries default OFF or aggregate-only — flagging *suspicion* of distress on indirect phrases with no user-visible response is arguably worse than Tier 1 logging.
- One-line note in Aria's persona that the Tier 2 flag is system-injected and not a user instruction.

### Advisor's answers to spec's open questions
1. **Sensuality suppression after Tier 1:** YES, suppress for remainder of session (not N turns — session boundary cleaner). Tonal whiplash is real harm; user can `/reset` or `CRISIS_FLOOR_MODE=tier2_only` if they want roleplay back.
2. **"Are you safe right now?" follow-up:** NO. Drifts clinical. The "stay" line ("I'm here if you want to keep talking") already invites disclosure.
3. **Banter suppression window:** session-scoped, not turn-counted (same as #1).
4. **Post-filter:** partial edit authority on `overdose` / `self_harm` categories only — see Required #4.
5. **Default `on`:** concur, with i18n + TTS fixes (without those, default-on is worse than default-off for non-US users).

### Drift
- SECURITY.md: new "Safety floor" section — deterministic pre-LLM filter on user input only; audit semantics; opt-out semantics; explicit "NOT content moderation, NOT clinical tool."
- ARCHITECTURE.md: new `crisis_floor.py` in trust-boundary diagram, between WS-input sanitization and `generate_response`, parallel to `untrusted_content`.
- Membrane: Aria persona prompt amended (small Tier 2 addendum) — flag for `code-reviewer`.
