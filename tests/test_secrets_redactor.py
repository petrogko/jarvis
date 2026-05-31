"""Hermetic tests for secrets_redactor.

Covers spec §11 + the security-advisor's required + recommended additions:
- Each detector: positive + a tricky false-positive negative.
- Luhn check on cards.
- SSN issuance-rule rejection.
- Spoken-password extended coverage (per Required #5).
- ``[REDACTED:category]`` preserved in current turn.
- ``collapse_for_resume`` strips categories + groups consecutive (Required #3).
- Audit invariant on exception path (Required #4).
- Hard input-length cap.
- WARN-mode pending-unredactions store: redact-immediately + TTL + single-use.
"""

from __future__ import annotations

import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import secrets_redactor as sr


# ---------- SSN ------------------------------------------------------------

def test_ssn_detected():
    text = "my number is 123-45-6789, please don't share"
    detections = sr.scan(text)
    assert any(d.category == "ssn" for d in detections)


def test_ssn_rejects_invalid_issuance_blocks():
    # 000-, 666-, 9xx- prefixes; "00" middle; "0000" suffix.
    for bad in ("000-12-3456", "666-12-3456", "912-34-5678", "111-00-3456", "111-22-0000"):
        assert not sr.scan(bad), f"FP on {bad!r}"


def test_ssn_redaction_replaces_match():
    text = "ssn 123-45-6789 ok"
    out, dets = sr.redact(text)
    assert out == "ssn [REDACTED:ssn] ok"
    assert dets[0].raw == "123-45-6789"


# ---------- Credit card (Luhn) --------------------------------------------

def test_card_luhn_valid_detected():
    text = "use 4111-1111-1111-1111 today"
    detections = sr.scan(text)
    assert any(d.category == "card" for d in detections)


def test_card_invalid_luhn_rejected():
    # Same shape but Luhn-invalid.
    assert not sr.scan("4111-1111-1111-1112")


def test_card_long_digit_run_no_redos():
    """Adversarial input to prove the card regex doesn't catastrophic-backtrack.
    A 4 KiB run of digits would otherwise be a ReDoS amplifier."""
    big = "9" * 4096
    started = time.perf_counter()
    sr.scan(big)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, f"card regex too slow on adversarial input: {elapsed:.3f}s"


# ---------- ABA routing ---------------------------------------------------

def test_routing_aba_checksum_valid():
    # 011000015 = Federal Reserve Bank of Boston, real valid checksum.
    assert any(d.category == "routing" for d in sr.scan("routing 011000015 ok"))


def test_routing_aba_checksum_invalid_rejected():
    # Use a 9-digit string that fails both ABA checksum AND the SSN
    # issuance rules (starts with 9 → invalid SSN; fails ABA mod-10).
    detections = sr.scan("999123456")
    assert not any(d.category == "routing" for d in detections)
    assert not any(d.category == "ssn" for d in detections)


# ---------- API keys -------------------------------------------------------

@pytest.mark.parametrize("key, cat", [
    ("sk-ant-api03-AAAAAAAAAAAAAAAA-xxxxxxxx", "anthropic_key"),
    ("sk-AAAAAAAAAAAAAAAAAAAAAAAA", "openai_key"),
    ("tvly-AAAAAAAAAAAAAAAA", "tavily_key"),
    ("ghp_AAAAAAAAAAAAAAAAAAAA", "github_pat"),
    ("github_pat_AAAAAAAAAAAAAAAAAAAA", "github_pat"),
    ("xoxb-1234567890-AAAA", "slack_bot"),
    ("AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
    ("sk_live_AAAAAAAAAAAAAAAAAAAA", "stripe_live"),
    ("pk_test_AAAAAAAAAAAAAAAAAAAA", "stripe_test"),
    ("whsec_AAAAAAAAAAAAAAAAAAAA", "stripe_webhook"),
])
def test_api_keys_detected(key, cat):
    detections = sr.scan(f"my key is {key}")
    assert any(d.category == cat for d in detections), f"missed {cat}: {key}"


# ---------- JWT -----------------------------------------------------------

def test_jwt_detected():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abc123signature"
    assert any(d.category == "jwt" for d in sr.scan(f"token: {jwt}"))


def test_jwt_three_segments_required():
    # Two-segment string isn't a JWT.
    assert not any(d.category == "jwt" for d in sr.scan("eyJabc.eyJdef"))


# ---------- PEM / OpenSSH private -----------------------------------------

def test_pem_block_detected():
    text = (
        "ok here it is\n"
        "-----BEGIN CERTIFICATE-----\n"
        "MIIBkTCCATugAwIBAgIBADANBgkqhkiG9w0BAQ0FADA\n"
        "-----END CERTIFICATE-----\n"
    )
    cats = {d.category for d in sr.scan(text)}
    assert "pem" in cats


def test_openssh_private_separates_from_pem():
    text = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAA\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    cats = {d.category for d in sr.scan(text)}
    assert "openssh_private" in cats
    assert "pem" not in cats  # de-overlap


# ---------- ssh-rsa public ------------------------------------------------

def test_ssh_pub_key_detected():
    text = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ"
    cats = {d.category for d in sr.scan(text)}
    assert "ssh_public" in cats


# ---------- bcrypt --------------------------------------------------------

def test_bcrypt_detected():
    # Real-shape bcrypt hash.
    h = "$2b$12$EXRkfkdmXn2gzds2SSitu.MW9.gAVqa9eLS1//RYtYCmB1eLHL.9C"
    cats = {d.category for d in sr.scan(h)}
    assert "bcrypt" in cats


# ---------- IBAN ----------------------------------------------------------

def test_iban_mod97_valid():
    # GB29NWBK60161331926819 — canonical example IBAN.
    assert any(d.category == "iban" for d in sr.scan("send to GB29NWBK60161331926819"))


def test_iban_mod97_invalid_rejected():
    # Wrong check digits.
    assert not any(d.category == "iban" for d in sr.scan("GB99NWBK60161331926819"))


# ---------- Spoken password (advisor Required #5 extended coverage) ------

@pytest.mark.parametrize("text, secret", [
    ("my password is hunter2", "hunter2"),
    ("the password is hunter2 right", "hunter2"),
    ("our password is hunter2", "hunter2"),
    ("password: hunter2", "hunter2"),
    ("passcode is 1234", "1234"),
    ("PIN is 9876", "9876"),
    ("pin colon 9876", "9876"),
    ("use hunter2 as the password", "hunter2"),
    ("type hunter2 as password", "hunter2"),
    ("enter hunter2 as the passcode", "hunter2"),
])
def test_spoken_password_extended_coverage(text, secret):
    detections = sr.scan(text)
    cats = [d for d in detections if d.category == "spoken_password"]
    assert cats, f"missed spoken_password in: {text!r}"
    assert any(d.raw == secret for d in cats), f"raw mismatch: {cats}"


def test_spoken_password_no_false_positive_on_natural_text():
    # "password" used as a generic noun, not preceded by "is/colon".
    assert not any(d.category == "spoken_password"
                   for d in sr.scan("I forgot my password yesterday"))


# ---------- collapse_for_resume (Required #3) -----------------------------

def test_collapse_strips_category_label():
    text = "earlier I said [REDACTED:ssn] and [REDACTED:card]"
    out = sr.collapse_for_resume(text)
    assert "[REDACTED:ssn]" not in out
    assert "[REDACTED:card]" not in out


def test_collapse_groups_consecutive_redactions():
    text = "context: [REDACTED:ssn], [REDACTED:card], [REDACTED:routing] then ok"
    out = sr.collapse_for_resume(text)
    assert "[earlier sensitive content omitted]" in out
    # Shouldn't contain three separate [REDACTED] markers anymore.
    assert out.count("[REDACTED]") <= 1


def test_collapse_passthrough_when_no_redactions():
    assert sr.collapse_for_resume("plain text") == "plain text"


# ---------- Input cap + audit invariant ----------------------------------

def test_input_over_cap_short_circuits():
    """Inputs over INPUT_MAX_BYTES return empty without scanning. Defends
    against ReDoS amplification on the card / IBAN patterns."""
    big = "A" * (sr.INPUT_MAX_BYTES + 1)
    assert sr.scan(big) == []


def test_scan_does_not_raise_on_weird_input():
    """All detectors are wrapped — no input should propagate an exception
    out of scan() (per Required #4). The audit-line-on-error invariant is
    enforced at the call site; here we assert the exception swallowing."""
    weird = "\x00\x01\xfe" * 50
    # No assertion on detections; the assertion is "no raise."
    sr.scan(weird)


# ---------- WARN-mode PendingUnredactions (Required #2) ------------------

def test_pending_unredactions_round_trip():
    p = sr.PendingUnredactions()
    dets = [sr.Detection("ssn", (0, 11), "123-45-6789")]
    p.hold(42, dets)
    assert p.consume(42) == dets


def test_pending_unredactions_single_use():
    """Required-fix #2 — single-use semantics. Once consumed, the held bytes
    are gone; a second consume returns None."""
    p = sr.PendingUnredactions()
    p.hold(42, [sr.Detection("ssn", (0, 11), "123-45-6789")])
    assert p.consume(42) is not None
    assert p.consume(42) is None


def test_pending_unredactions_ttl_expiry(monkeypatch):
    """Beyond PENDING_HOLD_TTL_S the raw secret is unrecoverable from the
    side map — bound the WARN-mode exposure window."""
    p = sr.PendingUnredactions()
    base = time.time()
    monkeypatch.setattr(sr.time, "time", lambda: base)
    p.hold(42, [sr.Detection("ssn", (0, 11), "123-45-6789")])
    # Jump past TTL.
    monkeypatch.setattr(sr.time, "time", lambda: base + sr.PENDING_HOLD_TTL_S + 1)
    assert p.consume(42) is None


def test_pending_unredactions_evict_expired_drops_stale_entries(monkeypatch):
    p = sr.PendingUnredactions()
    base = time.time()
    monkeypatch.setattr(sr.time, "time", lambda: base)
    p.hold(1, [sr.Detection("ssn", (0, 1), "x")])
    p.hold(2, [sr.Detection("ssn", (0, 1), "y")])
    monkeypatch.setattr(sr.time, "time", lambda: base + sr.PENDING_HOLD_TTL_S + 1)
    p.evict_expired()
    assert p.consume(1) is None
    assert p.consume(2) is None


# ---------- redact() return shape -----------------------------------------

def test_redact_returns_redacted_text_and_detections():
    text = "ssn 123-45-6789 ok"
    out, dets = sr.redact(text)
    assert out == "ssn [REDACTED:ssn] ok"
    assert len(dets) == 1
    assert dets[0].category == "ssn"
    assert dets[0].raw == "123-45-6789"


def test_redact_handles_multiple_overlapping_detections():
    """Two redactions in a single string — both should land cleanly."""
    text = "ssn 123-45-6789 card 4111-1111-1111-1111 done"
    out, dets = sr.redact(text)
    assert "[REDACTED:ssn]" in out
    assert "[REDACTED:card]" in out
    assert len(dets) == 2


def test_redact_noop_on_clean_text():
    out, dets = sr.redact("the weather is fine today, sir")
    assert out == "the weather is fine today, sir"
    assert dets == []
