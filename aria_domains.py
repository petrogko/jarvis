"""
Domain prior loader for Aria.

Each `aria_domains/<name>.md` brief teaches Aria how HE wants her to engage
in that domain — not general knowledge (Opus has it), but the frame, the
red flags, the questions HE wants her to ask. This module:

  1. Detects which domain(s) a user message touches via keyword regex
  2. Loads the matching briefs (cap 3 to avoid prompt bloat)
  3. Returns a single concatenated markdown string the server injects
     into the system prompt

Routing is deliberately keyword-only — no LLM classification, no
embeddings. Cheap, deterministic, easy to debug. False positives are
acceptable (loading a brief that doesn't apply is a small token cost,
not a behavior change — Aria reads it as background, like any prompt).
False negatives are also acceptable for now — Aria still has her base
prompt and Opus's general knowledge. Domains are a sharpening, not a
gating.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DOMAINS_DIR = Path(__file__).resolve().parent / "aria_domains"

# Per-domain keyword regex. Hits = include that brief. Tuned for moderate
# recall, not surgical precision — domains are additive, not exclusive.
_DOMAIN_KEYWORDS: dict[str, re.Pattern] = {
    "business": re.compile(
        r"\b("
        r"business|startup|company|founder|cofounder|ceo|cfo|coo|"
        r"revenue|arr|mrr|margin|burn|runway|"
        r"customer|customers|churn|retention|"
        r"deal|deals|term sheet|negotiat|"
        r"investor|investors|vc|series [a-d]|funding|raise|raised|"
        r"hire|hiring|fire|fired|layoff|laid off|team|"
        r"product|launch|roadmap|strategy|"
        r"board|advisor|advisors|"
        r"competitor|market|"
        r"acquisition|acquire|exit|sold the company|selling|sale"
        r")\b",
        re.IGNORECASE,
    ),
    "legal": re.compile(
        r"\b("
        r"contract|contracts|agreement|"
        r"clause|clauses|terms|"
        r"liability|indemnit|"
        r"non.?compete|nda|non.?solicit|"
        r"ip|intellectual property|trademark|patent|copyright|"
        r"lawsuit|sued|suing|legal|lawyer|attorney|counsel|"
        r"compliance|regulator|regulation|"
        r"license|licensing|"
        r"breach|terminate|termination|"
        r"jurisdiction|governing law|"
        r"sign(?:ing|ed)?\s+(?:a|the|this|that)\b"
        r")\b",
        re.IGNORECASE,
    ),
    "fatherhood": re.compile(
        r"\b("
        r"son|sons|daughter|daughters|kid|kids|child|children|"
        r"father|fatherhood|dad|parent|parenting|"
        r"my boy|my girl|my little|"
        r"family time|"
        r"school|teacher|grade|homework|"
        r"my (?:wife|partner|ex)\b|co.?parent"
        r")\b",
        re.IGNORECASE,
    ),
}

# Cache loaded briefs in memory — they don't change at runtime.
_BRIEF_CACHE: dict[str, str] = {}


def _load_brief(domain: str) -> Optional[str]:
    if domain in _BRIEF_CACHE:
        return _BRIEF_CACHE[domain]
    path = _DOMAINS_DIR / f"{domain}.md"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("aria_domains: failed to read %s: %s", path, e)
        return None
    _BRIEF_CACHE[domain] = text
    return text


def detect_domains(user_text: str, max_domains: int = 3) -> list[str]:
    """Return up to `max_domains` domain names whose keywords appear in
    `user_text`, in priority order (most matches first, then alphabetical
    for ties). Empty list for messages too short or with no matches."""
    if not user_text:
        return []
    t = user_text.strip()
    if len(t) < 12:
        return []
    scored: list[tuple[int, str]] = []
    for name, regex in _DOMAIN_KEYWORDS.items():
        matches = regex.findall(t)
        if matches:
            scored.append((len(matches), name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [name for _, name in scored[:max_domains]]


def build_domain_context(user_text: str) -> str:
    """Return the concatenated markdown of all domain briefs relevant to
    this turn, or empty string if none. Wraps each brief in a clear
    header so Aria knows where one brief ends and another begins."""
    domains = detect_domains(user_text)
    if not domains:
        return ""
    blocks: list[str] = []
    for d in domains:
        brief = _load_brief(d)
        if brief:
            blocks.append(f"--- DOMAIN BRIEF: {d.upper()} ---\n{brief}")
    return "\n\n".join(blocks)


def clear_cache() -> None:
    """For tests / hot-reload."""
    _BRIEF_CACHE.clear()
