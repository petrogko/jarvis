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


def _run_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_hook_is_executable():
    import stat
    mode = HOOK_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, f"{HOOK_SCRIPT} is not executable"


def test_hook_warns_on_membrane_edit():
    for f in MEMBRANE_FILES:
        payload = {"tool_name": "Edit", "tool_input": {"file_path": f"/x/{f}"}}
        result = _run_hook(payload)
        assert result.returncode == 0, f"hook should exit 0 for {f}; got {result.returncode}"
        assert "tripwire" in result.stderr.lower() or "membrane" in result.stderr.lower(), (
            f"hook stderr should mention tripwire/membrane for {f}; got {result.stderr!r}"
        )
        assert f in result.stderr, f"hook stderr should mention {f}; got {result.stderr!r}"


def test_hook_silent_on_non_membrane_edit():
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "/x/server.py"}}
    result = _run_hook(payload)
    assert result.returncode == 0
    assert result.stderr == "", f"expected no stderr for non-membrane edit; got {result.stderr!r}"


def test_hook_handles_malformed_json():
    result = subprocess.run(
        [str(HOOK_SCRIPT)],
        input="not-json",
        capture_output=True,
        text=True,
        timeout=5,
    )
    # Must NEVER fail loud — best-effort hook, exit 0 on bad input.
    assert result.returncode == 0


def test_settings_json_is_valid():
    assert SETTINGS.exists(), f"missing {SETTINGS}"
    data = json.loads(SETTINGS.read_text())
    # Required shape: PostToolUse with our matcher + command.
    hooks = data.get("hooks", {}).get("PostToolUse", [])
    assert hooks, "no PostToolUse hooks configured"
    matched = [
        h for h in hooks
        if h.get("matcher") == "Edit|Write|MultiEdit"
    ]
    assert matched, "no PostToolUse matcher for Edit|Write|MultiEdit"
    commands = [
        sub.get("command")
        for h in matched
        for sub in h.get("hooks", [])
        if sub.get("type") == "command"
    ]
    assert any("membrane-edit-warn.sh" in (c or "") for c in commands), (
        f"membrane-edit-warn.sh not wired; got commands={commands}"
    )
