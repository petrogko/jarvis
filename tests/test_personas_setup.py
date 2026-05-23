"""
Hermetic acceptance tests for the personas system.

Verifies static artifacts (agent files parse, settings.json valid,
hook script behaves, CLAUDE.md has the routing section). Does not
exercise the live Agent tool — that's verified manually per the
spec's acceptance criteria.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / ".claude" / "agents"
HOOK_SCRIPT = ROOT / "scripts" / "hooks" / "membrane-edit-warn.sh"
SETTINGS = ROOT / ".claude" / "settings.json"

PERSONAS = ("security-advisor", "software-architect", "code-reviewer", "test-runner", "controller")
MEMBRANE_FILES = ("SECURITY.md", "ARCHITECTURE.md", "auth.py")


def test_hook_script_exists():
    assert HOOK_SCRIPT.exists(), f"missing {HOOK_SCRIPT}"
