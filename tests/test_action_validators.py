"""
Tests for the action-target validators that gate every [ACTION:X]
emitted by the LLM. Run with pytest after installing requirements.txt
(server.py imports anthropic/fastapi).
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _import_server_or_skip():
    try:
        import server  # noqa: F401
        return sys.modules["server"]
    except ImportError:
        return None


def test_browse_accepts_http_https():
    s = _import_server_or_skip()
    if s is None:
        return
    assert s._is_browse_target_safe("http://example.com")
    assert s._is_browse_target_safe("https://example.com/path?q=1")


def test_browse_accepts_plain_search_string():
    s = _import_server_or_skip()
    if s is None:
        return
    assert s._is_browse_target_safe("best restaurants in austin")


def test_browse_rejects_dangerous_schemes():
    s = _import_server_or_skip()
    if s is None:
        return
    for bad in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "ftp://evil/",
        "jar:http://x!/y",
    ):
        assert not s._is_browse_target_safe(bad), f"should reject {bad!r}"


def test_browse_rejects_empty():
    s = _import_server_or_skip()
    if s is None:
        return
    assert not s._is_browse_target_safe("")


def test_project_name_validator():
    s = _import_server_or_skip()
    if s is None:
        return
    assert s._looks_like_safe_project_name("my-cool-app")
    assert s._looks_like_safe_project_name("The Client Engine")
    bad = [
        "a; rm -rf /",
        'foo"bar',
        "foo`id`",
        "foo$IFS",
        "foo|cat",
        "foo>out",
        "foo\nbar",
        "foo\x00bar",
        "",
        "x" * 300,
    ]
    for n in bad:
        assert not s._looks_like_safe_project_name(n), f"should reject {n!r}"


def test_validate_action_browse_with_bad_scheme():
    s = _import_server_or_skip()
    if s is None:
        return
    ok, _ = s.validate_action({"action": "browse", "target": "file:///etc/passwd"})
    assert not ok


def test_validate_action_prompt_project_with_metachars():
    s = _import_server_or_skip()
    if s is None:
        return
    ok, _ = s.validate_action({
        "action": "prompt_project",
        "target": "foo; rm -rf / ||| prompt",
    })
    assert not ok


def test_validate_action_oversize_target():
    s = _import_server_or_skip()
    if s is None:
        return
    ok, _ = s.validate_action({"action": "build", "target": "x" * 10_000})
    assert not ok


def test_extract_action_drops_injected_browse():
    s = _import_server_or_skip()
    if s is None:
        return
    response = "Sure, opening that. [ACTION:BROWSE] javascript:alert(1)"
    clean, action = s.extract_action(response)
    assert action is None, "extract_action should have suppressed the malicious BROWSE"
    assert "Sure, opening that." in clean


def test_extract_action_accepts_legitimate_browse():
    s = _import_server_or_skip()
    if s is None:
        return
    response = "Right away, sir. [ACTION:BROWSE] https://example.com"
    clean, action = s.extract_action(response)
    assert action is not None
    assert action["action"] == "browse"
    assert action["target"] == "https://example.com"
