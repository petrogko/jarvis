"""
Hermetic tests for jarvis_sidecar.auth.

The auth model is a single shared-secret token compared with constant-time
equality. Missing or wrong header → 401.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis_sidecar import auth


def test_constant_time_equal_matches_identical():
    assert auth._constant_time_equal("abc", "abc") is True


def test_constant_time_equal_rejects_different():
    assert auth._constant_time_equal("abc", "abd") is False


def test_constant_time_equal_rejects_different_lengths():
    assert auth._constant_time_equal("abc", "abcd") is False


def test_constant_time_equal_rejects_empty_inputs():
    assert auth._constant_time_equal("", "abc") is False
    assert auth._constant_time_equal("abc", "") is False
    assert auth._constant_time_equal("", "") is False
