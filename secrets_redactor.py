"""
Secrets-detection redactor — pre-filter at WS receive + pre-LLM boundary.

Spec: ``docs/superpowers/specs/2026-05-30-secrets-redactor-design.md``
Security-advisor: GO-WITH-FIXES (2026-05-30). All 5 required fixes folded in:

  1. Assistant-reply path: ``extract_action`` must run BEFORE redact (server-side
     ordering, enforced at the call site in ``server.py``).
  2. WARN mode never holds raw secrets in ``history`` — the in-history string is
     replaced with the redacted form IMMEDIATELY; the raw bytes live only in
     the short-lived ``pending_unredactions`` map with hard TTL + single-use.
  3. ``[REDACTED:category]`` is preserved within the same turn (Aria needs to
     reason); ``collapse_for_resume`` strips category labels and groups
     consecutive redactions for resume-load context.
  4. Audit-log invariant extends to exception paths — ``try/except`` wraps
     every detector call; on error the audit line carries ``exc_type`` only,
     never message or traceback.
  5. Spoken-password coverage extended: "the password is X", "use X as the
     password/PIN", "type X" / "enter X", "passcode is", "PIN colon".

Plus advisor's recommended additional categories: JWT, PEM blocks, OpenSSH
private keys, spoken ``ssh-rsa``, bcrypt, IBAN (with mod-97 validator).

Pre-compiled patterns at module load. Hard input-length cap (4 KiB) and a
per-call wall-clock guard defend against ReDoS on the catastrophic-backtrack
candidates (cards, IBAN).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("jarvis.secrets_redactor")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Detection:
    """One secret found by the scanner. The ``raw`` field is held only in
    process memory between detection and redaction; it MUST NOT be logged
    or persisted under any code path."""

    category: str       # "ssn" | "card" | "routing" | "api_key" | "jwt" | ...
    span: tuple[int, int]  # (start, end) byte offsets in the input string
    raw: str            # the actual matched bytes — handle with care


# ---------------------------------------------------------------------------
# Limits + config
# ---------------------------------------------------------------------------

# Hard input-length cap. Larger inputs short-circuit to "no scan" — the
# upstream STT/LLM paths bound their own sizes anyway, and going past this
# is a ReDoS amplification vector on the card / IBAN patterns.
INPUT_MAX_BYTES = 4 * 1024  # 4 KiB

# Per-call wall-clock guard. The scan loop checks this after each detector
# and bails with a warning if exceeded. Defends against an adversarial input
# that triggers catastrophic backtracking on a single regex.
SCAN_WALL_CLOCK_S = 0.05  # 50 ms — well above the p95 target of 10 ms

# WARN-mode raw-secret hold TTL. After this, the pending_unredactions entry
# is evicted and the redaction stands.
PENDING_HOLD_TTL_S = 60.0


# ---------------------------------------------------------------------------
# Detectors — precompiled patterns
# ---------------------------------------------------------------------------

# SSN: ``\b\d{3}-?\d{2}-?\d{4}\b`` then validator excluding known invalid
# issuance blocks (000-, 666-, 9xx-, and the "no all-zeros segment" rule).
_SSN_RE = re.compile(r"\b(\d{3})-?(\d{2})-?(\d{4})\b")

# Credit card. 13-19 digit run with optional spaces/dashes; Luhn-validated
# downstream. Anchored at digit boundaries; the `{12,18}` inner cap limits
# the worst-case state explosion.
_CARD_RE = re.compile(r"\b(\d[ -]?){12,18}\d\b")

# US bank routing (ABA). Nine digits, validated via mod-10 checksum.
_ABA_RE = re.compile(r"\b\d{9}\b")

# API keys by prefix.
_API_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_key",  re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("openai_key",     re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("tavily_key",     re.compile(r"\btvly-[A-Za-z0-9]{16,}\b")),
    ("github_pat",     re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b")),
    ("slack_bot",      re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,}\b")),
    ("slack_user",     re.compile(r"\bxoxe-[A-Za-z0-9-]{10,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("stripe_live",    re.compile(r"\b(?:sk|pk|rk)_live_[A-Za-z0-9]{20,}\b")),
    ("stripe_test",    re.compile(r"\b(?:sk|pk|rk)_test_[A-Za-z0-9]{20,}\b")),
    ("stripe_webhook", re.compile(r"\bwhsec_[A-Za-z0-9]{20,}\b")),
)

# JWT — three base64url-segments separated by `.`. The middle segment starts
# with the literal "eyJ" (base64 of `{"`) which dramatically reduces FPs.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")

# PEM blocks — any begin/end pair. Multiline; we match the BEGIN line and
# carry through to the END line.
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z][A-Z ]+-----.*?-----END [A-Z][A-Z ]+-----",
    re.DOTALL,
)

# OpenSSH private key (narrower than PEM).
_OPENSSH_PRIV_RE = re.compile(
    r"-----BEGIN OPENSSH PRIVATE KEY-----.*?-----END OPENSSH PRIVATE KEY-----",
    re.DOTALL,
)

# Spoken/typed ssh public key.
_SSH_PUB_RE = re.compile(r"\bssh-(?:rsa|ed25519|ecdsa)\s+[A-Za-z0-9+/=]{20,}")

# bcrypt hash.
_BCRYPT_RE = re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}")

# IBAN — country code (2 letters) + 2 check digits + 11-30 alphanumerics.
# Validated downstream via mod-97.
_IBAN_RE = re.compile(r"\b([A-Z]{2})(\d{2})([A-Z0-9]{11,30})\b")

# Spoken-password patterns — natural language the user might say into the
# mic. Per advisor Required #5, extended past the spec's original "my
# password is X" form.
_SPOKEN_PASSWORD_RE = re.compile(
    # The separator between "password" and the secret varies: "password is X"
    # needs whitespace before "is"; "password: X" allows zero space before ":";
    # "password colon X" needs whitespace. Hence the alternation.
    r"(?:my |the |our )?(?:password|passcode|passphrase|pin)"
    r"(?:\s+is\b|\s*[:]|\s+colon\b)\s+(\S+)",
    re.IGNORECASE,
)
_SPOKEN_PASSWORD_USE_RE = re.compile(
    r"(?:use|type|enter)\s+(\S+)\s+(?:as\s+(?:the\s+)?(?:password|passcode|passphrase|pin))",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _luhn(digits: str) -> bool:
    """Mod-10 checksum used by credit cards."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if d < 0 or d > 9:
            return False
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _ssn_valid(a: str, b: str, c: str) -> bool:
    """SSA issuance rules — reject the invariant-invalid blocks."""
    if a == "000" or a == "666" or a.startswith("9"):
        return False
    if b == "00":
        return False
    if c == "0000":
        return False
    return True


def _aba_valid(d: str) -> bool:
    """ABA routing checksum: 3·(d0+d3+d6) + 7·(d1+d4+d7) + (d2+d5+d8) % 10 == 0."""
    if len(d) != 9 or not d.isdigit():
        return False
    total = (
        3 * (int(d[0]) + int(d[3]) + int(d[6]))
        + 7 * (int(d[1]) + int(d[4]) + int(d[7]))
        + (int(d[2]) + int(d[5]) + int(d[8]))
    )
    return total % 10 == 0


def _iban_valid(country: str, check: str, body: str) -> bool:
    """ISO 13616 mod-97 checksum."""
    rearranged = body + country + check
    # Letters → digits A=10..Z=35.
    numeric = []
    for ch in rearranged:
        if ch.isdigit():
            numeric.append(ch)
        else:
            numeric.append(str(ord(ch) - ord("A") + 10))
    try:
        return int("".join(numeric)) % 97 == 1
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Scanner — one detector per category
# ---------------------------------------------------------------------------

def _scan_ssn(text: str) -> list[Detection]:
    out = []
    for m in _SSN_RE.finditer(text):
        if _ssn_valid(m.group(1), m.group(2), m.group(3)):
            out.append(Detection("ssn", m.span(), m.group(0)))
    return out


def _scan_card(text: str) -> list[Detection]:
    out = []
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            out.append(Detection("card", m.span(), m.group(0)))
    return out


def _scan_routing(text: str) -> list[Detection]:
    out = []
    for m in _ABA_RE.finditer(text):
        if _aba_valid(m.group(0)):
            out.append(Detection("routing", m.span(), m.group(0)))
    return out


def _scan_api_keys(text: str) -> list[Detection]:
    out = []
    for category, pattern in _API_KEY_PATTERNS:
        for m in pattern.finditer(text):
            out.append(Detection(category, m.span(), m.group(0)))
    return out


def _scan_jwt(text: str) -> list[Detection]:
    return [Detection("jwt", m.span(), m.group(0)) for m in _JWT_RE.finditer(text)]


def _scan_pem(text: str) -> list[Detection]:
    out = []
    # OpenSSH private is a stricter subset; match it first.
    for m in _OPENSSH_PRIV_RE.finditer(text):
        out.append(Detection("openssh_private", m.span(), m.group(0)))
    # General PEM — skip ranges already covered by openssh.
    covered = {(d.span[0], d.span[1]) for d in out}
    for m in _PEM_RE.finditer(text):
        if m.span() not in covered:
            out.append(Detection("pem", m.span(), m.group(0)))
    return out


def _scan_ssh_pub(text: str) -> list[Detection]:
    return [Detection("ssh_public", m.span(), m.group(0)) for m in _SSH_PUB_RE.finditer(text)]


def _scan_bcrypt(text: str) -> list[Detection]:
    return [Detection("bcrypt", m.span(), m.group(0)) for m in _BCRYPT_RE.finditer(text)]


def _scan_iban(text: str) -> list[Detection]:
    out = []
    for m in _IBAN_RE.finditer(text):
        if _iban_valid(m.group(1), m.group(2), m.group(3)):
            out.append(Detection("iban", m.span(), m.group(0)))
    return out


def _scan_spoken_password(text: str) -> list[Detection]:
    out = []
    for m in _SPOKEN_PASSWORD_RE.finditer(text):
        # Span covers just the captured secret, not the lead-in.
        out.append(Detection("spoken_password", m.span(1), m.group(1)))
    for m in _SPOKEN_PASSWORD_USE_RE.finditer(text):
        out.append(Detection("spoken_password", m.span(1), m.group(1)))
    return out


# Ordered list — earlier detectors run first; later ones can be cheaper to skip.
_DETECTORS = (
    ("openssh_private/pem", _scan_pem),
    ("jwt",                 _scan_jwt),
    ("api_key",             _scan_api_keys),
    ("ssn",                 _scan_ssn),
    ("card",                _scan_card),
    ("routing",             _scan_routing),
    ("ssh_public",          _scan_ssh_pub),
    ("bcrypt",              _scan_bcrypt),
    ("iban",                _scan_iban),
    ("spoken_password",     _scan_spoken_password),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan(text: str) -> list[Detection]:
    """Return all detections in ``text`` — sorted by start offset, de-overlapped.

    Defense-in-depth invariants (per security-advisor required fix #4):
    - Hard input-length cap; over-cap inputs short-circuit with no scan.
    - Per-call wall-clock guard.
    - Each detector wrapped in ``try/except``; on error the audit line carries
      only the exception class name. NEVER the input, NEVER a traceback.
    """
    if not text or len(text.encode("utf-8")) > INPUT_MAX_BYTES:
        return []

    started = time.perf_counter()
    detections: list[Detection] = []

    for category_label, fn in _DETECTORS:
        try:
            detections.extend(fn(text))
        except re.error as e:
            # NEVER log the input or a traceback.
            log.warning(
                "secrets_redactor: regex error in detector %s (exc=%s)",
                category_label, type(e).__name__,
            )
        except Exception as e:  # noqa: BLE001 — broad on purpose
            log.warning(
                "secrets_redactor: detector %s raised %s",
                category_label, type(e).__name__,
            )

        if time.perf_counter() - started > SCAN_WALL_CLOCK_S:
            log.warning(
                "secrets_redactor: wall-clock guard tripped at detector %s",
                category_label,
            )
            break

    # Sort + de-overlap. If two detections overlap, keep the earlier one
    # (it's usually the more specific category — pem before bcrypt etc).
    detections.sort(key=lambda d: (d.span[0], -d.span[1]))
    out: list[Detection] = []
    last_end = -1
    for d in detections:
        if d.span[0] >= last_end:
            out.append(d)
            last_end = d.span[1]
    return out


def redact(text: str) -> tuple[str, list[Detection]]:
    """Return ``(redacted_text, detections)``. Each match is replaced with
    ``[REDACTED:<category>]`` per spec §4 — the category survives within the
    same turn so the LLM has enough context to reason about what was removed.

    On resume-load, call ``collapse_for_resume`` to strip the category labels
    (per security-advisor required fix #3).
    """
    detections = scan(text)
    if not detections:
        return text, []

    # Replace from end so spans stay valid.
    out = text
    for d in reversed(detections):
        out = out[: d.span[0]] + f"[REDACTED:{d.category}]" + out[d.span[1]:]
    return out, detections


_REDACTED_TOKEN_RE = re.compile(r"\[REDACTED(?::[a-z_]+)?\]")
_CONSECUTIVE_REDACTIONS_RE = re.compile(r"(?:\[REDACTED\][\s,;]*){2,}")


def collapse_for_resume(text: str) -> str:
    """Strip category labels and group consecutive redactions on resume.

    Per security-advisor required fix #3: keeping `[REDACTED:ssn]` in the
    current turn is fine because the LLM needs context. On resume from the
    persisted store, the category labels become an inferential aggregation
    risk ("four [REDACTED:ssn] → user has many SSNs to discuss") so we
    collapse to neutral filler.
    """
    if not text:
        return text
    # Strip category suffix.
    out = _REDACTED_TOKEN_RE.sub("[REDACTED]", text)
    # Group consecutive `[REDACTED]` blobs into a single placeholder.
    out = _CONSECUTIVE_REDACTIONS_RE.sub("[earlier sensitive content omitted] ", out)
    return out


# ---------------------------------------------------------------------------
# WARN-mode pending-unredactions side map (per security-advisor required fix #2)
# ---------------------------------------------------------------------------

class PendingUnredactions:
    """Short-lived store of raw matched secrets keyed by ``conversation_id``.

    Required-fix #2 is the load-bearing reason this class exists: in WARN
    mode the in-history string is replaced with the redacted form IMMEDIATELY
    so the next LLM turn ships the redacted version. The raw bytes only live
    here, behind a hard TTL + single-use semantics, in case the user says
    "no don't redact that" on the very next turn.

    Not thread-safe; one instance per server process is sufficient since the
    WS handler runs on the asyncio loop.
    """

    def __init__(self):
        self._entries: dict[int, tuple[float, list[Detection]]] = {}

    def hold(self, conversation_id: int, detections: list[Detection]) -> None:
        if not detections:
            return
        self._entries[conversation_id] = (time.time() + PENDING_HOLD_TTL_S, detections)

    def consume(self, conversation_id: int) -> Optional[list[Detection]]:
        """Single-use retrieval. Returns the held detections or None if
        expired / missing / already consumed."""
        entry = self._entries.pop(conversation_id, None)
        if entry is None:
            return None
        expires_at, detections = entry
        if time.time() > expires_at:
            return None
        return detections

    def evict_expired(self) -> None:
        now = time.time()
        expired = [cid for cid, (exp, _) in self._entries.items() if now > exp]
        for cid in expired:
            del self._entries[cid]
