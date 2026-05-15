"""
Regression tests for the AppleScript injection hardening.

These tests assert structural properties of the refactored modules:
the scripts that previously interpolated untrusted strings now use
``on run argv`` and pass values via osascript argv. We do not invoke
osascript itself — the harness is OS-agnostic.

Run with:  pytest tests/test_applescript_safety.py
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ACTIONS = (ROOT / "actions.py").read_text()
NOTES = (ROOT / "notes_access.py").read_text()
MAIL = (ROOT / "mail_access.py").read_text()
CALENDAR = (ROOT / "calendar_access.py").read_text()


# ---------------------------------------------------------------------------
# Negative assertions — the dangerous patterns are gone
# ---------------------------------------------------------------------------

_DANGEROUS_REPLACE = re.compile(r'\.replace\(\s*[\'"]"[\'"]\s*,\s*[\'"]\\\\"[\'"]\s*\)')


def test_actions_does_not_use_naive_quote_escape():
    assert not _DANGEROUS_REPLACE.search(ACTIONS), (
        "actions.py still contains the unsafe '.replace(\\\"\\\", \\\"\\\\\\\\\\\\\\\"\\\")' pattern"
    )


def test_notes_does_not_use_naive_quote_escape():
    assert not _DANGEROUS_REPLACE.search(NOTES)


def test_mail_does_not_use_naive_quote_escape():
    assert not _DANGEROUS_REPLACE.search(MAIL)


# ---------------------------------------------------------------------------
# Positive assertions — argv-passing is in use
# ---------------------------------------------------------------------------

def test_actions_run_osascript_helper_exists():
    assert "async def run_osascript(" in ACTIONS
    assert "on run argv" in ACTIONS


def test_notes_uses_argv_for_untrusted_inputs():
    # All three previously-vulnerable functions should use argv-passing.
    for func in ("async def read_note(", "async def search_notes_apple(", "async def create_apple_note("):
        idx = NOTES.find(func)
        assert idx >= 0, f"{func} missing"
        body = NOTES[idx:idx + 1500]
        assert "on run argv" in body, f"{func} should declare on run argv"
        assert "args=" in body, f"{func} should pass args= to _run_notes_script"


def test_mail_uses_argv_for_untrusted_inputs():
    for func in ("async def get_messages_from_account(", "async def search_mail(", "async def read_message("):
        idx = MAIL.find(func)
        assert idx >= 0, f"{func} missing"
        body = MAIL[idx:idx + 2000]
        assert "on run argv" in body, f"{func} should declare on run argv"
        assert "args=" in body, f"{func} should pass args= to _run_mail_script"


def test_calendar_bulk_script_uses_argv():
    assert "on run argv" in CALENDAR
    assert "calendar calName" in CALENDAR


# ---------------------------------------------------------------------------
# Boundary validator — shell metacharacters rejected
# ---------------------------------------------------------------------------

def test_assert_safe_path_rejects_metacharacters():
    sys.path.insert(0, str(ROOT))
    if "actions" in sys.modules:
        del sys.modules["actions"]
    import actions  # type: ignore

    actions._assert_safe_path("/Users/me/Desktop/my-project")  # ok

    bad = [
        "/tmp/foo; rm -rf /",
        "/tmp/foo && cat /etc/passwd",
        '/tmp/foo"',
        "/tmp/foo\nrm -rf",
        "/tmp/foo`id`",
        "/tmp/foo$IFS",
        "/tmp/foo|cat",
        "/tmp/foo>out",
        "",
    ]
    for p in bad:
        try:
            actions._assert_safe_path(p)
        except ValueError:
            continue
        raise AssertionError(f"_assert_safe_path should have rejected {p!r}")


# ---------------------------------------------------------------------------
# Helper passes args via osascript "--" separator
# ---------------------------------------------------------------------------

def test_run_osascript_invokes_subprocess_with_args(monkeypatch=None):
    """Without actually launching osascript, verify the argv shape."""
    if "actions" in sys.modules:
        del sys.modules["actions"]
    import actions  # type: ignore

    captured: dict = {}

    async def fake_exec(*cmd, **kw):
        captured["cmd"] = cmd

        class _P:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return _P()

    actions.asyncio.create_subprocess_exec = fake_exec  # type: ignore

    async def run():
        await actions.run_osascript("on run argv\n  return item 1 of argv\nend run", ["arg-one", "arg two"])

    asyncio.run(run())

    cmd = captured["cmd"]
    assert cmd[0] == "osascript"
    assert cmd[1] == "-e"
    # The "--" separator must come before user args so they can't be
    # interpreted as osascript options.
    sep_idx = cmd.index("--")
    assert cmd[sep_idx + 1] == "arg-one"
    assert cmd[sep_idx + 2] == "arg two"
