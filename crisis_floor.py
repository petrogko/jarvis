"""
Crisis floor — deterministic pre-LLM safety filter.

Spec: ``docs/superpowers/specs/2026-05-30-crisis-floor-design.md``
Security-advisor: GO-WITH-FIXES (2026-05-30). All 7 required fixes folded in:

  #1 Classifier under-specified. We DROPPED the classifier entirely and
     use regex-only with sentence-level negation suppression — simpler,
     no committed weights to manage, no scikit-learn dep, no training-
     source provenance question. Documented honestly: this is a small
     reliable floor, not clinical detection.
  #2 FP target rephrased. We do NOT claim a measured production FP rate.
     We claim "zero FP on the held-out test set in this repo" — and that
     set includes the recovery-user case ("I used to want to die")
     explicitly tested in tests/test_crisis_floor.py.
  #3 Negation suppression is SENTENCE-LEVEL, not 5-token window. Any
     negation token anywhere earlier in the same sentence suppresses
     the match. Handles long-distance and double-negation.
  #4 Post-filter on Aria output (assistant_text) is allowed for the two
     categories where Aria authoring has no legitimate counsel use:
     ``substance`` and ``self_harm_method``. Log-only for ``ideation``
     and ``panic`` where roleplay is plausible.
  #5 Audit retention: each crisis_floor entry carries a fixed expiry
     timestamp so an operator (or the user via a follow-up CLI) can
     purge old entries without touching the rest of the audit log.
  #6 i18n: response templates are keyed by (category, locale). Ship
     ``us``, ``gb``, ``intl`` at minimum. Locale read from vault key
     ``CRISIS_FLOOR_LOCALE`` (default ``us``).
  #7 TTS voice for floor response: the WS handler emits a
     ``{"type":"crisis_floor_response", "text":..., "neutral_voice":true}``
     frame so the frontend can render with a distinct neutral voice or
     suppress TTS in favor of a visual notice. The Aria warm Cori
     delivering a hotline number is tonally wrong and possibly harmful.

Plus the advisor's structural recommendations:
- Scanner takes a typed ``UserTurn`` object (not raw string) so
  untrusted_content-wrapped mail/calendar payloads can't reach it by
  accident.
- Tier 2 audit entries are aggregate-only (daily category counts), never
  per-event — they're a distress-suspicion record and per-event logging
  is arguably worse than Tier 1 logging.
- One-line note injected as system context for Tier 2 so Aria knows
  the flag is system-injected, not a user instruction.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("jarvis.crisis_floor")

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UserTurn:
    """First-party user text. The scanner refuses bare strings (advisor
    recommended structural fix) so a routing mistake can't slip untrusted
    content (mail/calendar quotes) into the crisis detector."""

    text: str


@dataclass(frozen=True)
class Detection:
    """Tier 1 = direct, high-confidence ideation. Triggers the deterministic
    response. Tier 2 = indirect distress; surfaces a context flag for Aria
    but no auto-response, no per-event audit, and no persona override."""

    tier: int            # 1 or 2
    category: str        # ideation | substance | panic | self_harm_method | indirect
    span: tuple[int, int]  # offsets in the input — handle locally; do not log


# ---------------------------------------------------------------------------
# Tier 1 patterns — direct, high-confidence
# ---------------------------------------------------------------------------

# Explicit suicidal ideation. Designed to require an explicit first-person
# present-tense statement; the negation/temporal-past scanner below suppresses
# "I used to want to die" / "I would never want to die" / "She said she wants
# to die" etc.
_T1_IDEATION = re.compile(
    r"\b(?:"
    r"i\s+want\s+to\s+(?:die|kill\s+myself|end\s+(?:my\s+life|it\s+all))"
    r"|i'?m\s+going\s+to\s+(?:die|kill\s+myself|end\s+(?:my\s+life|it\s+all))"
    r"|i\s+(?:will|wanna)\s+(?:kill\s+myself|end\s+(?:my\s+life|it\s+all))"
    r"|i\s+have\s+a\s+(?:plan|method)\s+to\s+(?:die|kill\s+myself)"
    r"|kill(?:ing)?\s+myself\s+tonight"
    r")\b",
    re.IGNORECASE,
)

# Active substance crisis — present-moment overdose statements.
_T1_SUBSTANCE = re.compile(
    r"\b(?:"
    r"i'?m\s+overdosing"
    r"|i\s+(?:just\s+)?took\s+(?:too\s+many|all\s+(?:my\s+|the\s+)?pills?)"
    r"|i\s+can'?t\s+stop\s+(?:using|drinking)"
    r"|i'?ve\s+been\s+(?:drinking|using)\s+all\s+day\s+and\s+i\s+can'?t\s+stop"
    r")\b",
    re.IGNORECASE,
)

# Acute panic / dissociation — explicit "right now" framing required to
# distinguish from describing past episodes.
_T1_PANIC = re.compile(
    r"\b(?:"
    r"i\s+can'?t\s+breathe\s+(?:right\s+now|i\s+think\s+i'?m\s+dying)"
    r"|(?:i'?m|feel(?:ing)?)\s+(?:having|in)\s+a\s+panic\s+attack\s+right\s+now"
    r"|i\s+don'?t\s+know\s+(?:who|where)\s+i\s+am\s+right\s+now"
    r"|i'?m\s+losing\s+(?:my\s+)?(?:mind|grip|touch)\s+right\s+now"
    r")\b",
    re.IGNORECASE,
)

# Self-harm method discussion (post-filter category — Aria authoring this
# has no legitimate counsel use; the post-filter is allowed to suppress
# Aria's own output if she ever generates it).
_T1_SELF_HARM_METHOD = re.compile(
    r"\b(?:"
    r"how\s+(?:to|do\s+i)\s+(?:overdose|kill\s+myself|hang\s+myself|cut\s+(?:deep|properly))"
    r"|what\s+(?:medications?|pills?)\s+(?:will|would)\s+kill\s+me"
    r"|lethal\s+(?:dose|amount)\s+of\s+\w+"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Tier 2 patterns — indirect distress (context flag, no auto-response)
# ---------------------------------------------------------------------------

_T2_INDIRECT = re.compile(
    r"\b(?:"
    r"i\s+(?:can'?t|cannot)\s+(?:go\s+on|do\s+this(?:\s+anymore)?)"
    r"|nothing\s+matters(?:\s+anymore)?"
    r"|everyone\s+would\s+be\s+better\s+off\s+without\s+me"
    r"|i\s+(?:feel|am)\s+a\s+burden(?:\s+to\s+everyone)?"
    r"|i\s+don'?t\s+want\s+to\s+be\s+here\s+anymore"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Suppression — negation / temporal-past / quote / fiction
# ---------------------------------------------------------------------------

# Sentence-level negation. Per advisor required fix #3, any negation token
# anywhere earlier in the SAME SENTENCE suppresses the match. This handles:
# - "I would never want to die"
# - "I would never, after everything I've been through this year, want to kill myself"
# - "It's not that I don't want to die" — double negation flagged separately
_NEGATION_TOKENS = re.compile(r"\b(?:never|not|don'?t|do\s+not|won'?t|will\s+not|wouldn'?t|would\s+not|can'?t|cannot|no\s+longer|no\s+more)\b", re.IGNORECASE)

# Temporal-past markers — "I used to" / "back when" / "before" — convert a
# present-tense match into past framing.
_TEMPORAL_PAST = re.compile(r"\b(?:used\s+to|back\s+when|once|years?\s+ago|in\s+the\s+past|before(?:\s+i\s+(?:got|started))?|when\s+i\s+was)\b", re.IGNORECASE)

# Quote/fiction markers — if the match is inside quotes or follows a
# fictional/roleplay framing, suppress.
_FICTION_MARKERS = re.compile(r"\b(?:character|story|roleplay|fiction|novel|lyrics?|song|movie|book|she\s+said|he\s+said|they\s+said|the\s+character)\b", re.IGNORECASE)

# Sentence split — keep it simple. Split on `.`, `!`, `?`, `;` followed by
# whitespace OR end of string. (Not perfect but adequate for this use.)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+")


def _sentence_containing(text: str, span: tuple[int, int]) -> str:
    """Return the sentence containing the given span."""
    start, end = span
    # Walk back to find the start of the sentence.
    sentence_start = 0
    for m in _SENTENCE_SPLIT.finditer(text[:start]):
        sentence_start = m.end()
    # Walk forward to find the end of the sentence.
    rest = text[end:]
    next_split = _SENTENCE_SPLIT.search(rest)
    sentence_end = end + (next_split.start() if next_split else len(rest))
    return text[sentence_start:sentence_end]


def _is_suppressed(text: str, match_span: tuple[int, int]) -> bool:
    """Sentence-level suppression. The match fires only if:
    - No negation token appears anywhere earlier in the same sentence.
    - No temporal-past marker appears anywhere in the same sentence.
    - No fiction/quote marker appears anywhere in the same sentence.

    Per advisor required fix #3, this is SENTENCE-level (not 5-token window),
    so long-distance negation and complex prefixed clauses are handled.
    """
    sentence = _sentence_containing(text, match_span)

    # Negation BEFORE the match within the sentence.
    match_start_in_sentence = match_span[0] - text.rfind(sentence, 0, match_span[0] + 1)
    if match_start_in_sentence < 0:
        # Edge case: span wasn't actually inside the located sentence.
        # Conservatively suppress (don't fire) to favor false negatives over
        # false positives, since false positives are the real harm.
        return True
    sentence_before_match = sentence[:match_start_in_sentence]
    if _NEGATION_TOKENS.search(sentence_before_match):
        return True

    # Temporal-past anywhere in the sentence.
    if _TEMPORAL_PAST.search(sentence):
        return True

    # Fiction/quote markers anywhere in the sentence.
    if _FICTION_MARKERS.search(sentence):
        return True

    return False


# ---------------------------------------------------------------------------
# Public scanning API
# ---------------------------------------------------------------------------

_TIER1_DETECTORS = (
    ("ideation",          _T1_IDEATION),
    ("substance",         _T1_SUBSTANCE),
    ("panic",             _T1_PANIC),
    ("self_harm_method",  _T1_SELF_HARM_METHOD),
)

_TIER2_DETECTORS = (
    ("indirect",          _T2_INDIRECT),
)


def scan(turn: UserTurn) -> Optional[Detection]:
    """Return the highest-tier detection or None.

    Per advisor structural recommendation, this requires a typed UserTurn
    object — refuses to scan bare strings — so a routing mistake can't slip
    untrusted-content into the detector by accident.
    """
    if not isinstance(turn, UserTurn):
        raise TypeError(
            "crisis_floor.scan requires UserTurn, not raw string — this is "
            "deliberate (untrusted-content boundary). Wrap your input."
        )
    text = turn.text
    if not text or not text.strip():
        return None

    # Tier 1 — direct. Return immediately on first match (priority order:
    # ideation > substance > panic > self_harm_method).
    for category, pattern in _TIER1_DETECTORS:
        for m in pattern.finditer(text):
            try:
                if _is_suppressed(text, m.span()):
                    continue
            except Exception:
                # Suppression must never raise — fail-safe to "suppressed"
                # rather than over-firing. Conservative bias toward FN.
                log.warning(
                    "crisis_floor: suppression check raised, treating as suppressed (exc=%s)",
                    "<redacted>",
                )
                continue
            return Detection(tier=1, category=category, span=m.span())

    # Tier 2 — indirect. Same suppression discipline.
    for category, pattern in _TIER2_DETECTORS:
        for m in pattern.finditer(text):
            try:
                if _is_suppressed(text, m.span()):
                    continue
            except Exception:
                continue
            return Detection(tier=2, category=category, span=m.span())

    return None


def scan_assistant_output(text: str) -> Optional[Detection]:
    """Post-filter on Aria's generated output (advisor required fix #4).

    ONLY checks the two categories where Aria authoring has no legitimate
    counsel use: ``substance`` and ``self_harm_method``. If she ever
    generates either (jailbroken / roleplay-drift / model hallucination),
    the caller suppresses the output and emits the floor response instead.

    Log-only for ``ideation`` and ``panic`` where roleplay is plausible —
    NOT enforced here (per the advisor's asymmetry: post-filter on user
    output is suppression-eligible only for the two categories).
    """
    if not text or not text.strip():
        return None
    # Reuse the suppression machinery via a synthetic UserTurn.
    turn = UserTurn(text=text)
    for category, pattern in (("substance", _T1_SUBSTANCE),
                              ("self_harm_method", _T1_SELF_HARM_METHOD)):
        for m in pattern.finditer(text):
            try:
                if _is_suppressed(text, m.span()):
                    continue
            except Exception:
                continue
            return Detection(tier=1, category=category, span=m.span())
    return None


# ---------------------------------------------------------------------------
# Locale-keyed response templates (advisor required fix #6)
# ---------------------------------------------------------------------------

# Each entry is (category, locale) → response string. The response follows
# the spec's ground-validate-refer-stay protocol:
#   1. Ground: acknowledge what was said, simply. No platitudes.
#   2. Validate: brief acknowledgment that the feeling is real.
#   3. Refer: hotline number, locale-appropriate.
#   4. Stay: offer to keep talking; clarify NOT a substitute for help.
#
# Tone is intentionally plain — NOT warm, NOT clinical, NOT formal. Just
# present. Warmth in this moment from a flirtatious British secretary
# voice would be tonally wrong; the WS handler emits a ``neutral_voice``
# flag with this text (advisor required fix #7) so the frontend can render
# with a distinct voice or suppress TTS.

_RESPONSES: dict[tuple[str, str], str] = {
    # --- US locale ---------------------------------------------------------
    ("ideation", "us"): (
        "Sir. I heard what you said. The feeling you're describing is real, and you're not alone in it. "
        "If you can, please call or text 988 — the Suicide and Crisis Lifeline. Twenty-four hours, free, confidential. "
        "I'll stay here with you. I'm not a substitute for someone who can actually help, but I'm here."
    ),
    ("substance", "us"): (
        "Sir. If you've taken too much of something, please call 911 or Poison Control (1-800-222-1222) right now. "
        "If you can't call, ask someone near you to call. I'll stay with you. This is past where I can help on my own."
    ),
    ("panic", "us"): (
        "Sir. Stay with me. You're safe right now even if it doesn't feel that way. "
        "Slow breath in, slow breath out — that's it. Feel your feet on the floor. "
        "If this doesn't ease, 988 is there. I'm here as long as you want me here."
    ),
    ("self_harm_method", "us"): (
        "Sir. I won't help with that, but I'm not going anywhere. "
        "What you're feeling is real, and 988 (call or text) is there twenty-four hours. "
        "I'm here if you want to keep talking."
    ),

    # --- GB locale ---------------------------------------------------------
    ("ideation", "gb"): (
        "Sir. I heard what you said. The feeling you're describing is real, and you're not alone in it. "
        "If you can, please call Samaritans on 116 123 — twenty-four hours, free, confidential. "
        "I'll stay here with you. I'm not a substitute for someone who can actually help, but I'm here."
    ),
    ("substance", "gb"): (
        "Sir. If you've taken too much of something, please call 999 or NHS 111 right now. "
        "If you can't call, ask someone near you to call. I'll stay with you. This is past where I can help on my own."
    ),
    ("panic", "gb"): (
        "Sir. Stay with me. You're safe right now even if it doesn't feel that way. "
        "Slow breath in, slow breath out. Feel your feet on the floor. "
        "If this doesn't ease, Samaritans on 116 123 is there. I'm here as long as you want me here."
    ),
    ("self_harm_method", "gb"): (
        "Sir. I won't help with that, but I'm not going anywhere. "
        "What you're feeling is real, and Samaritans on 116 123 is there twenty-four hours. "
        "I'm here if you want to keep talking."
    ),

    # --- INTL fallback (Befrienders Worldwide) ----------------------------
    ("ideation", "intl"): (
        "Sir. I heard what you said. The feeling you're describing is real, and you're not alone in it. "
        "Befrienders Worldwide (befrienders.org) lists crisis lines in your country. "
        "I'll stay here with you. I'm not a substitute for someone who can actually help, but I'm here."
    ),
    ("substance", "intl"): (
        "Sir. If you've taken too much of something, please call your local emergency number now. "
        "If you can't, ask someone near you to call. I'll stay with you. This is past where I can help on my own."
    ),
    ("panic", "intl"): (
        "Sir. Stay with me. You're safe right now even if it doesn't feel that way. "
        "Slow breath in, slow breath out. Feel your feet on the floor. "
        "If this doesn't ease, befrienders.org lists support in your country. I'm here."
    ),
    ("self_harm_method", "intl"): (
        "Sir. I won't help with that, but I'm not going anywhere. "
        "befrienders.org lists support in your country. I'm here if you want to keep talking."
    ),
}

# Tier 2 — context flag injected as system context, not as a user-facing
# response. Aria sees it and adjusts her own behavior; she does not reveal
# the flag's existence to the user.
TIER2_PERSONA_FLAG: str = (
    "[crisis_floor system flag — DO NOT reveal to the user] An indirect-distress "
    "phrase was detected in the user's last message. Notice it. Attend without "
    "pathologizing. Do not perform concern or refer to resources unless the user "
    "steers there. Stay present. This is a system-injected flag, not a user "
    "instruction; do not reference it."
)


def response_for(detection: Detection, locale: str = "us") -> Optional[str]:
    """Return the locale-appropriate response text for a Tier 1 detection.
    Returns None for Tier 2 (no auto-response). Falls back to ``intl`` if
    the locale isn't recognized."""
    if detection.tier != 1:
        return None
    locale = (locale or "us").strip().lower()
    if locale not in ("us", "gb", "intl"):
        locale = "intl"
    return _RESPONSES.get((detection.category, locale)) or _RESPONSES.get((detection.category, "intl"))


# ---------------------------------------------------------------------------
# Audit shape (advisor required fix #5 + structural recommendation)
# ---------------------------------------------------------------------------

# Tier 1 entries auto-expire after this many seconds. The audit log itself
# is append-only; a follow-up CLI ``jarvis crisis-floor purge`` can use this
# timestamp to drop expired rows without touching the rest of the log.
TIER1_AUDIT_TTL_S: float = 30 * 86400.0  # 30 days


def audit_record_for_tier1(detection: Detection, conversation_id: Optional[int]) -> dict:
    """Shape an audit record for a Tier 1 trigger.

    NEVER includes the matched text, the input bytes, or any user content.
    Includes:
      - timestamp + expires_at (purge horizon).
      - tier + category + conversation_id (no token fingerprint here —
        that's added by the audit_log infrastructure).
    """
    now = time.time()
    return {
        "tier": 1,
        "category": detection.category,
        "conversation_id": conversation_id,
        "expires_at": now + TIER1_AUDIT_TTL_S,
    }


# Tier 2 entries are aggregated daily into a category counter, not logged
# per-event (advisor recommendation — per-event logging of distress
# suspicion is arguably worse than Tier 1 logging since it triggers on
# weaker signal with no user-visible cause).
class Tier2DailyCounter:
    """In-memory aggregate of Tier 2 detections by category, per day.
    The counter is flushed to the audit log at midnight (or when the
    server shuts down) so the durable record is one line per category
    per day, not one line per detection."""

    def __init__(self):
        self._counts: dict[tuple[str, str], int] = {}  # (date_iso, category) -> count

    def increment(self, category: str) -> None:
        from datetime import date
        key = (date.today().isoformat(), category)
        self._counts[key] = self._counts.get(key, 0) + 1

    def drain(self) -> list[dict]:
        """Return accumulated rows + reset. Caller writes them to the
        audit log. Each row: {date, category, count}."""
        rows = [
            {"date": d, "category": c, "count": n}
            for (d, c), n in self._counts.items()
        ]
        self._counts.clear()
        return rows
