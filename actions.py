"""
JARVIS Action Executor — AppleScript-based system actions.

Execute actions IMMEDIATELY, before generating any LLM response.
Each function returns {"success": bool, "confirmation": str}.

Security:
    Untrusted values (URLs, project names, prompts) MUST be passed to
    AppleScript via `run_osascript` argv — never interpolated into the
    script source. The script reads them as `item N of argv` inside an
    `on run argv ... end run` handler, so a `"`, `\\n`, or `\\` in the
    value cannot break out of the AppleScript string literal and inject
    additional statements. Functions whose AppleScript uses `do script`
    are *intentional shell exec*: callers must additionally validate
    that the input is shell-safe (e.g. fixed string, regex-restricted
    project name, etc).
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

log = logging.getLogger("jarvis.actions")

DESKTOP_PATH = Path.home() / "Desktop"

# Project-dir whitelist: alnum, dash, underscore, slash, dot. No quotes,
# no shell metacharacters, no whitespace. Anchored at start/end.
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


def _assert_safe_path(p: str, label: str = "path") -> None:
    """Refuse paths that contain shell or AppleScript metacharacters.

    Used at the boundary into any AppleScript that will `do script` the
    value into a shell. Keeps the shell-injection class closed even if
    a future caller forgets to sanitize.
    """
    if not p or not _SAFE_PATH_RE.match(p):
        raise ValueError(f"unsafe {label}: contains disallowed characters: {p!r}")


async def run_osascript(
    script: str,
    args: Iterable[str] = (),
    *,
    timeout: float | None = None,
) -> tuple[int, bytes, bytes]:
    """Run osascript with untrusted values passed via argv.

    The script must use ``on run argv ... end run`` and reference values
    as ``item N of argv``. The script source itself MUST be a constant
    (no f-string interpolation of caller-supplied data); only the args
    list is allowed to carry untrusted values.
    """
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, "--", *[str(a) for a in args],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if timeout is not None:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    else:
        stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


async def _mark_terminal_as_jarvis(revert_after: float = 5.0):
    """Temporarily set the front Terminal window to Ocean theme, then revert.

    Shows the user JARVIS is active in that terminal. Reverts after revert_after seconds.
    """
    # Save the current profile, switch to Ocean, then revert
    script_save = (
        'tell application "Terminal"\n'
        '    return name of current settings of front window\n'
        'end tell'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script_save,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        original_profile = stdout.decode().strip()

        # Switch to Ocean
        script_set = (
            'tell application "Terminal"\n'
            '    set current settings of front window to settings set "Ocean"\n'
            'end tell'
        )
        proc2 = await asyncio.create_subprocess_exec(
            "osascript", "-e", script_set,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc2.communicate()

        # Schedule revert
        if original_profile and original_profile != "Ocean":
            asyncio.get_event_loop().call_later(
                revert_after,
                lambda: asyncio.ensure_future(_revert_terminal_theme(original_profile))
            )
    except Exception:
        pass


_REVERT_THEME_SCRIPT = '''
on run argv
    set profileName to item 1 of argv
    tell application "Terminal"
        set current settings of front window to settings set profileName
    end tell
end run
'''


async def _revert_terminal_theme(profile_name: str):
    """Revert a Terminal window back to its original profile."""
    try:
        await run_osascript(_REVERT_THEME_SCRIPT, [profile_name])
    except Exception:
        pass


_OPEN_TERMINAL_WITH_CMD = '''
on run argv
    set cmd to item 1 of argv
    tell application "Terminal"
        activate
        do script cmd
    end tell
end run
'''

_OPEN_TERMINAL_BARE = '''
tell application "Terminal"
    activate
end tell
'''


async def open_terminal(command: str = "") -> dict:
    """Open Terminal.app and optionally run a command. Marks it blue for JARVIS.

    SECURITY: ``do script`` executes ``command`` as shell in Terminal.
    Callers MUST pass either a fixed literal string or a value validated
    upstream. argv-passing here closes the AppleScript-escape class, but
    not the underlying shell-exec primitive.
    """
    if command:
        rc, _, stderr = await run_osascript(_OPEN_TERMINAL_WITH_CMD, [command])
    else:
        rc, _, stderr = await run_osascript(_OPEN_TERMINAL_BARE)
    success = rc == 0
    if not success:
        log.error(f"open_terminal failed: {stderr.decode()}")
    else:
        await _mark_terminal_as_jarvis()
    return {
        "success": success,
        "confirmation": "Terminal is open, sir." if success else "I had trouble opening Terminal, sir.",
    }


_OPEN_FIREFOX_SCRIPT = '''
on run argv
    set theURL to item 1 of argv
    tell application "Firefox"
        activate
        open location theURL
    end tell
end run
'''

_OPEN_CHROME_SCRIPT = '''
on run argv
    set theURL to item 1 of argv
    tell application "Google Chrome"
        activate
        open location theURL
    end tell
end run
'''


async def open_browser(url: str, browser: str = "chrome") -> dict:
    """Open URL in user's browser (Chrome or Firefox)."""
    if browser.lower() == "firefox":
        app_name = "Firefox"
        rc, _, stderr = await run_osascript(_OPEN_FIREFOX_SCRIPT, [url])
    else:
        app_name = "Chrome"
        rc, _, stderr = await run_osascript(_OPEN_CHROME_SCRIPT, [url])
    success = rc == 0
    if not success:
        log.error(f"open_browser ({app_name}) failed: {stderr.decode()}")
    return {
        "success": success,
        "confirmation": f"Pulled that up in {app_name}, sir." if success else f"{app_name} ran into a problem, sir.",
    }


# Keep backward compat
async def open_chrome(url: str) -> dict:
    return await open_browser(url, "chrome")


_CLAUDE_IN_PROJECT_SCRIPT = '''
on run argv
    set cmd to item 1 of argv
    tell application "Terminal"
        activate
        do script cmd
    end tell
end run
'''


async def open_claude_in_project(project_dir: str, prompt: str) -> dict:
    """Open Terminal, cd to project dir, run Claude Code interactively.

    Writes the prompt to CLAUDE.md (which claude reads automatically on startup)
    then launches claude in interactive mode with --dangerously-skip-permissions.
    No prompt escaping needed — CLAUDE.md handles context delivery.
    """
    # Write prompt to CLAUDE.md — claude reads this automatically
    claude_md = Path(project_dir) / "CLAUDE.md"
    claude_md.write_text(f"# Task\n\n{prompt}\n\nBuild this completely. If web app, make index.html work standalone.\n")

    # ``project_dir`` is concatenated into a shell command; reject anything
    # outside the safe-path allowlist before letting it near a shell.
    _assert_safe_path(project_dir, "project_dir")
    cmd = f"cd {project_dir} && claude --dangerously-skip-permissions"
    rc, _, stderr = await run_osascript(_CLAUDE_IN_PROJECT_SCRIPT, [cmd])
    success = rc == 0
    if not success:
        log.error(f"open_claude_in_project failed: {stderr.decode()}")
    else:
        await _mark_terminal_as_jarvis()
    return {
        "success": success,
        "confirmation": "Claude Code is running in Terminal, sir. You can watch the progress."
        if success
        else "Had trouble spawning Claude Code, sir.",
    }


_PROMPT_EXISTING_TERMINAL_SCRIPT = '''
on run argv
    set targetName to item 1 of argv
    set userPrompt to item 2 of argv
    tell application "Terminal"
        set matched to false
        set targetWindow to missing value
        repeat with w in windows
            if name of w contains targetName then
                set targetWindow to w
                set matched to true
                exit repeat
            end if
        end repeat

        if not matched then
            return "NOT_FOUND"
        end if

        set index of targetWindow to 1
        set selected tab of targetWindow to selected tab of targetWindow
        activate
    end tell

    delay 1

    tell application "System Events"
        tell process "Terminal"
            set frontmost to true
            delay 0.3
            keystroke userPrompt
            delay 0.2
            keystroke return
        end tell
    end tell

    return "OK"
end run
'''


async def prompt_existing_terminal(project_name: str, prompt: str) -> dict:
    """Find a Terminal window matching a project name and type a prompt into it.

    Uses System Events keystroke to type into an active Claude Code session
    rather than `do script` which would open a new shell.

    SECURITY: ``project_name`` and ``prompt`` are passed via osascript argv;
    they cannot break out of AppleScript string context. ``keystroke`` types
    them as user input into whatever window is frontmost — which is
    sensitive in its own right, but no longer a source-injection vector.
    """
    try:
        rc, stdout, stderr = await run_osascript(
            _PROMPT_EXISTING_TERMINAL_SCRIPT,
            [project_name, prompt],
            timeout=15,
        )
        result = stdout.decode().strip()
        if result == "NOT_FOUND":
            return {
                "success": False,
                "confirmation": f"Couldn't find a terminal for {project_name}, sir.",
            }

        success = rc == 0
        if not success:
            log.error(f"prompt_existing_terminal failed: {stderr.decode()[:200]}")

        if success:
            await _mark_terminal_as_jarvis()

        return {
            "success": success,
            "confirmation": f"Sent that to {project_name}, sir." if success
            else f"Had trouble typing into {project_name}, sir.",
        }

    except asyncio.TimeoutError:
        return {"success": False, "confirmation": "Terminal operation timed out, sir."}
    except Exception as e:
        log.error(f"prompt_existing_terminal failed: {e}")
        return {"success": False, "confirmation": "Something went wrong reaching that terminal, sir."}


async def get_chrome_tab_info() -> dict:
    """Read the current Chrome tab's title and URL via AppleScript."""
    script = (
        'tell application "Google Chrome"\n'
        "    set tabTitle to title of active tab of front window\n"
        "    set tabURL to URL of active tab of front window\n"
        '    return tabTitle & "|" & tabURL\n'
        "end tell"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            result = stdout.decode().strip()
            parts = result.split("|", 1)
            if len(parts) == 2:
                return {"title": parts[0], "url": parts[1]}
        return {}
    except Exception as e:
        log.warning(f"get_chrome_tab_info failed: {e}")
        return {}


async def monitor_build(project_dir: str, ws=None, synthesize_fn=None) -> None:
    """Monitor a Claude Code build for completion. Notify via WebSocket when done."""
    import base64

    output_file = Path(project_dir) / ".jarvis_output.txt"
    start = time.time()
    timeout = 600  # 10 minutes

    while time.time() - start < timeout:
        await asyncio.sleep(5)
        if output_file.exists():
            content = output_file.read_text()
            if "--- JARVIS TASK COMPLETE ---" in content:
                log.info(f"Build complete in {project_dir}")
                if ws and synthesize_fn:
                    try:
                        msg = "The build is complete, sir."
                        audio_bytes = await synthesize_fn(msg)
                        if audio_bytes:
                            encoded = base64.b64encode(audio_bytes).decode()
                            await ws.send_json({"type": "status", "state": "speaking"})
                            await ws.send_json({"type": "audio", "data": encoded, "text": msg})
                            await ws.send_json({"type": "status", "state": "idle"})
                    except Exception as e:
                        log.warning(f"Build notification failed: {e}")
                return

    log.warning(f"Build timed out in {project_dir}")


async def execute_action(intent: dict, projects: list = None) -> dict:
    """Route a classified intent to the right action function.

    Args:
        intent: {"action": str, "target": str} from classify_intent()
        projects: list of known project dicts for resolving working dirs

    Returns: {"success": bool, "confirmation": str, "project_dir": str | None}
    """
    action = intent.get("action", "chat")
    target = intent.get("target", "")

    if action == "open_terminal":
        result = await open_terminal("claude --dangerously-skip-permissions")
        result["project_dir"] = None
        return result

    elif action == "browse":
        if target.startswith("http://") or target.startswith("https://"):
            url = target
        else:
            url = f"https://www.google.com/search?q={quote(target)}"

        # Detect which browser user wants
        target_lower = target.lower()
        if "firefox" in target_lower:
            browser = "firefox"
        else:
            browser = "chrome"

        result = await open_browser(url, browser)
        result["project_dir"] = None
        return result

    elif action == "build":
        # Create project folder on Desktop, spawn Claude Code
        project_name = _generate_project_name(target)
        project_dir = str(DESKTOP_PATH / project_name)
        os.makedirs(project_dir, exist_ok=True)
        result = await open_claude_in_project(project_dir, target)
        result["project_dir"] = project_dir
        return result

    else:
        return {"success": False, "confirmation": "", "project_dir": None}


def _generate_project_name(prompt: str) -> str:
    """Generate a kebab-case project folder name from the prompt."""
    # First: check for a quoted name like "tiktok-analytics-dashboard"
    quoted = re.search(r'"([^"]+)"', prompt)
    if quoted:
        name = quoted.group(1).strip()
        # Already kebab-case or close to it
        name = re.sub(r"[^a-zA-Z0-9\s-]", "", name).strip()
        if name:
            return re.sub(r"[\s]+", "-", name.lower())

    # Second: check for "called X" or "named X" pattern
    called = re.search(r'(?:called|named)\s+(\S+(?:[-_]\S+)*)', prompt, re.IGNORECASE)
    if called:
        name = re.sub(r"[^a-zA-Z0-9-]", "", called.group(1))
        if len(name) > 3:
            return name.lower()

    # Fallback: extract meaningful words
    words = re.sub(r"[^a-zA-Z0-9\s]", "", prompt.lower()).split()
    skip = {"a", "the", "an", "me", "build", "create", "make", "for", "with", "and",
            "to", "of", "i", "want", "need", "new", "project", "directory", "called",
            "on", "desktop", "that", "application", "app", "full", "stack", "simple",
            "web", "page", "site", "named"}
    meaningful = [w for w in words if w not in skip and len(w) > 2][:4]
    return "-".join(meaningful) if meaningful else "jarvis-project"
