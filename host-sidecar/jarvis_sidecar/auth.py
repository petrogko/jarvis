"""
Shared-secret token auth for the sidecar.

The token is read once at app startup. Subsequent comparisons use
hmac.compare_digest for constant-time equality (defense against timing
oracles).
"""

from __future__ import annotations

import hmac
from typing import Final

# Header name — distinct from the browser↔JARVIS X-JARVIS-Token, per
# security-advisor non-blocking recommendation #3 (disambiguation).
HEADER_NAME: Final[str] = "X-SIDECAR-Token"


def _constant_time_equal(a: str, b: str) -> bool:
    """Constant-time string comparison. Returns False on length mismatch."""
    if not a or not b or len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


def header_matches(expected: str, presented: str | None) -> bool:
    """True iff `presented` equals `expected` in constant time."""
    if presented is None:
        return False
    return _constant_time_equal(expected, presented)
