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


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)\Z", flags=re.DOTALL)

REQUIRED_AGENT_FIELDS = ("name", "description", "model")


def _parse_agent_file(path: pathlib.Path) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text) for an agent .md file.

    Parses YAML-ish frontmatter as ``key: value`` pairs without
    importing PyYAML — the agent files use a strict, small subset.
    """
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise AssertionError(f"{path}: no YAML frontmatter found")
    raw, body = match.group(1), match.group(2)
    front: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        front[key.strip()] = val.strip()
    return front, body


def _assert_persona_file_valid(name: str) -> None:
    path = AGENTS_DIR / f"{name}.md"
    assert path.exists(), f"missing {path}"
    front, body = _parse_agent_file(path)
    for field in REQUIRED_AGENT_FIELDS:
        assert field in front, f"{path}: missing required field {field!r}"
    assert front["name"] == name, f"{path}: name field must be {name!r}, got {front['name']!r}"
    assert front["model"] in ("opus", "sonnet", "haiku"), (
        f"{path}: model must be opus|sonnet|haiku, got {front['model']!r}"
    )
    assert len(body.strip()) >= 200, (
        f"{path}: body suspiciously short ({len(body.strip())} chars); a real system prompt"
        " should describe the persona's scope, output format, and constraints"
    )


def test_security_advisor_file_valid():
    _assert_persona_file_valid("security-advisor")


def test_software_architect_file_valid():
    _assert_persona_file_valid("software-architect")


def test_code_reviewer_file_valid():
    _assert_persona_file_valid("code-reviewer")


def test_test_runner_file_valid():
    _assert_persona_file_valid("test-runner")


def test_controller_file_valid():
    _assert_persona_file_valid("controller")


def test_controller_has_agent_tool():
    front, _ = _parse_agent_file(AGENTS_DIR / "controller.md")
    tools = front.get("tools", "")
    assert "Agent" in tools, (
        f"controller must have Agent tool to dispatch personas; got tools={tools!r}"
    )


def test_claude_md_has_persona_routing_section():
    text = (ROOT / "CLAUDE.md").read_text()
    assert "## Persona Routing" in text, "CLAUDE.md missing '## Persona Routing' section"
    for name in PERSONAS:
        assert name in text, f"CLAUDE.md routing section does not mention {name}"
    for name in MEMBRANE_FILES:
        assert name in text, f"CLAUDE.md routing section does not mention {name}"


def test_architecture_md_links_to_routing():
    text = (ROOT / "ARCHITECTURE.md").read_text()
    assert "Persona Routing" in text or "persona routing" in text, (
        "ARCHITECTURE.md should reference the persona routing in CLAUDE.md"
    )


def test_other_personas_do_not_have_agent_tool():
    # Only the controller dispatches. Other personas are leaves.
    for name in ("security-advisor", "software-architect", "code-reviewer", "test-runner"):
        front, _ = _parse_agent_file(AGENTS_DIR / f"{name}.md")
        tools = front.get("tools", "")
        assert "Agent" not in tools, (
            f"{name} must NOT have Agent tool (only controller dispatches); got tools={tools!r}"
        )


def test_all_five_personas_present():
    """Aggregate guard — fail loudly if any persona file is missing."""
    missing = [p for p in PERSONAS if not (AGENTS_DIR / f"{p}.md").exists()]
    assert not missing, f"missing personas: {missing}"


def test_no_jarvis_runtime_change():
    """The personas PR must not change JARVIS runtime imports.

    This is a structural test: if a future change to this PR adds
    a new top-level import to server.py, this test should be
    re-evaluated. Right now we assert no new persona module is
    imported by server.py.
    """
    text = (ROOT / "server.py").read_text()
    forbidden = (
        "from .claude",
        "import .claude",
        "from scripts.hooks",
        "import scripts.hooks",
        "from personas",
        "import personas",
    )
    for needle in forbidden:
        assert needle not in text, (
            f"server.py must not import persona infrastructure ({needle!r})"
        )
