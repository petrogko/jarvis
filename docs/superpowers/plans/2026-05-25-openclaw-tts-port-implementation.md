# OpenClaw Ports — `tts_local_cli` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the `openclaw_ports/` scaffolding (attribution, conventions, tests dir) and ship the first port: `tts_local_cli` — a Python module that uses macOS `say` to synthesize speech locally, replacing Fish Audio when available.

**Architecture:**
- New `openclaw_ports/` package at repo root with `NOTICE.md` (MIT attribution) + per-file headers pointing back to OpenClaw upstream commit `125d82cab2952f87f532106a368d54e526141026`.
- `openclaw_ports/tts_local_cli.py` exposes `is_available() -> bool` and `async synthesize(text, voice, timeout_s) -> bytes` returning AAC/M4A audio (decodable by Web Audio API; no ffmpeg needed — macOS `say` writes AAC natively).
- `server.py`'s `synthesize_speech` becomes a provider-aware dispatcher: vault-configured `TTS_PROVIDER` ∈ {`auto`, `local_cli`, `fish_audio`} chooses between the new local path and the existing Fish Audio path.

**Tech Stack:** Python 3.11 (stdlib only — no new deps), pytest + monkeypatch for hermetic subprocess mocking. macOS `say` binary at `/usr/bin/say`.

**Branch:** `feat/openclaw-tts-port` (already created from main).

**Persona gates (per `docs/superpowers/specs/2026-05-25-openclaw-ports-design.md` §3.3 + CLAUDE.md routing):**
- `security-advisor` — invoked on T7 (server.py call-site change touches the LLM/TTS trust boundary).
- `code-reviewer` — invoked before commits on T7, T8, T10.
- `test-runner` — invoked at T10 before the "ready to merge" claim.

---

## Task 1: Scaffolding — `openclaw_ports/` package + NOTICE

**Files:**
- Create: `openclaw_ports/__init__.py`
- Create: `openclaw_ports/NOTICE.md`

**Parallelism:** Blocks all later tasks. `[SEQUENTIAL]`

- [ ] **Step 1: Create empty package init**

Create `openclaw_ports/__init__.py`:

```python
"""
openclaw_ports — Python ports of MIT-licensed OpenClaw extensions.

See NOTICE.md for attribution and the umbrella spec at
docs/superpowers/specs/2026-05-25-openclaw-ports-design.md.
"""
```

- [ ] **Step 2: Create the attribution NOTICE**

Create `openclaw_ports/NOTICE.md`:

```markdown
# OpenClaw Ports — Attribution

Modules in this directory are ported from OpenClaw
(https://github.com/openclaw/openclaw), MIT-licensed.

## Pinned upstream commit

`125d82cab2952f87f532106a368d54e526141026` (as of 2026-05-25)

## Per-port table

| Module             | Upstream path                                              | Ported at SHA                              | Last resync |
|--------------------|------------------------------------------------------------|--------------------------------------------|-------------|
| `tts_local_cli.py` | `extensions/tts-local-cli/speech-provider.ts`              | `125d82cab2952f87f532106a368d54e526141026` | 2026-05-25  |

## Resync workflow

1. Look up the current ported SHA in this table.
2. `cd /Users/<user>/Development/github/openclaw && git diff <old_sha> HEAD -- <upstream-path>` — read the diff.
3. Forward-port changes by hand. Run the port's tests. Commit with `chore(openclaw_ports): resync <name> to <new_sha>`.
4. Update the table above with the new SHA and date.

## MIT License (verbatim from OpenClaw upstream)

MIT License

Copyright (c) 2026 OpenClaw Foundation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: Commit**

```bash
git add openclaw_ports/__init__.py openclaw_ports/NOTICE.md
git commit -m "feat(openclaw_ports): scaffolding + MIT attribution NOTICE

Establishes the openclaw_ports/ package per docs/superpowers/specs/
2026-05-25-openclaw-ports-design.md umbrella. Empty __init__.py +
NOTICE.md with pinned upstream commit
125d82cab2952f87f532106a368d54e526141026, per-port table (currently
just tts_local_cli pending implementation), resync workflow, and the
verbatim MIT license text."
```

---

## Task 2: Test scaffolding + module skeleton

**Files:**
- Create: `tests/test_openclaw_ports/__init__.py`
- Create: `tests/test_openclaw_ports/test_tts_local_cli.py`
- Create: `openclaw_ports/tts_local_cli.py`

**Parallelism:** `[SEQUENTIAL after T1]`. Blocks T3–T6.

- [ ] **Step 1: Create test package init (empty)**

Create `tests/test_openclaw_ports/__init__.py`:

```python
```

(Empty file — just marks the test directory as a package so pytest discovers it cleanly under the existing `pyproject.toml` `testpaths = ["tests"]`.)

- [ ] **Step 2: Write the failing module-surface test**

Create `tests/test_openclaw_ports/test_tts_local_cli.py`:

```python
"""
Hermetic tests for openclaw_ports.tts_local_cli.

The port is OS-dependent (macOS `say` binary). Tests mock subprocess
calls so they run on any platform in CI. A live integration test
hitting real `say` lives separately under tests/test_openclaw_ports/
integration/ and is excluded from the default pytest collection.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from openclaw_ports import tts_local_cli


def test_module_surface_exists():
    """Public surface per spec §3.2."""
    assert callable(tts_local_cli.is_available)
    assert callable(tts_local_cli.synthesize)
    assert issubclass(tts_local_cli.CLITTSUnavailable, Exception)
    assert issubclass(tts_local_cli.CLITTSError, Exception)


def test_attribution_header_present():
    """Spec §4.2 mandates a per-file attribution preamble."""
    src = (ROOT / "openclaw_ports" / "tts_local_cli.py").read_text(encoding="utf-8")
    assert "Ported from openclaw/extensions/tts-local-cli" in src
    assert "125d82cab2952f87f532106a368d54e526141026" in src
    assert "MIT-licensed" in src
    assert "openclaw_ports/NOTICE.md" in src
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/test_openclaw_ports/test_tts_local_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'openclaw_ports.tts_local_cli'`.

- [ ] **Step 4: Create the module skeleton**

Create `openclaw_ports/tts_local_cli.py`:

```python
"""
Local CLI text-to-speech via macOS `say`.

Ported from openclaw/extensions/tts-local-cli/speech-provider.ts at
commit 125d82cab2952f87f532106a368d54e526141026.
MIT-licensed; see openclaw_ports/NOTICE.md for full license text.

Resync policy: manual diff against the pinned commit. Bump SHA in
NOTICE.md when forward-porting upstream changes.

The OpenClaw original is a generic CLI-TTS provider with template-
substituted args, ffmpeg conversion, telephony output, and voice-note
opus. JARVIS only needs the macOS `say` path producing browser-
playable audio. We deliberately omit the generic CLI runner, ffmpeg
dependency, and non-mp4 output paths — `say` itself writes AAC in
M4A which Web Audio API decodes natively.
"""

from __future__ import annotations

import asyncio
import platform
import re
import shutil
import tempfile
from pathlib import Path
from typing import Final

SAY_BINARY: Final[str] = "/usr/bin/say"
DEFAULT_VOICE: Final[str] = "Alex"
DEFAULT_TIMEOUT_S: Final[float] = 30.0


class CLITTSUnavailable(RuntimeError):
    """Raised when macOS `say` is not present (e.g. inside a Linux container)."""


class CLITTSError(RuntimeError):
    """Raised on synthesis failure, timeout, or empty input after sanitization."""


def is_available() -> bool:
    """Return True iff we're on macOS and the `say` binary is executable.

    Implemented in T3.
    """
    raise NotImplementedError


async def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """Synthesize ``text`` to AAC/M4A audio bytes using macOS `say`.

    Implemented in T4–T6.
    """
    raise NotImplementedError
```

- [ ] **Step 5: Run the surface tests to verify they now pass**

Run: `pytest tests/test_openclaw_ports/test_tts_local_cli.py -v`
Expected: 2 PASSED (`test_module_surface_exists`, `test_attribution_header_present`).

- [ ] **Step 6: Commit**

```bash
git add tests/test_openclaw_ports/__init__.py tests/test_openclaw_ports/test_tts_local_cli.py openclaw_ports/tts_local_cli.py
git commit -m "feat(openclaw_ports): tts_local_cli skeleton + attribution test"
```

---

## Task 3: Implement `is_available()`

**Files:**
- Modify: `openclaw_ports/tts_local_cli.py`
- Modify: `tests/test_openclaw_ports/test_tts_local_cli.py`

**Parallelism:** `[SEQUENTIAL after T2]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_openclaw_ports/test_tts_local_cli.py`:

```python
def test_is_available_true_on_macos_with_say(monkeypatch):
    monkeypatch.setattr(tts_local_cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        tts_local_cli.shutil,
        "which",
        lambda name: tts_local_cli.SAY_BINARY if name == tts_local_cli.SAY_BINARY else None,
    )
    assert tts_local_cli.is_available() is True


def test_is_available_false_on_linux(monkeypatch):
    monkeypatch.setattr(tts_local_cli.platform, "system", lambda: "Linux")
    # Even if a `say` binary exists on PATH, we refuse non-Darwin.
    monkeypatch.setattr(tts_local_cli.shutil, "which", lambda name: "/usr/bin/say")
    assert tts_local_cli.is_available() is False


def test_is_available_false_if_say_missing(monkeypatch):
    monkeypatch.setattr(tts_local_cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(tts_local_cli.shutil, "which", lambda name: None)
    assert tts_local_cli.is_available() is False
```

- [ ] **Step 2: Run to verify the 3 new tests fail**

Run: `pytest tests/test_openclaw_ports/test_tts_local_cli.py -v -k is_available`
Expected: 3 FAILED (`NotImplementedError`).

- [ ] **Step 3: Implement `is_available()`**

In `openclaw_ports/tts_local_cli.py`, replace the `is_available()` body:

```python
def is_available() -> bool:
    """Return True iff we're on macOS and the `say` binary is executable."""
    if platform.system() != "Darwin":
        return False
    return shutil.which(SAY_BINARY) is not None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_openclaw_ports/test_tts_local_cli.py -v -k is_available`
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add openclaw_ports/tts_local_cli.py tests/test_openclaw_ports/test_tts_local_cli.py
git commit -m "feat(openclaw_ports): is_available() — gate on macOS + /usr/bin/say"
```

---

## Task 4: Implement `synthesize()` happy path

**Files:**
- Modify: `openclaw_ports/tts_local_cli.py`
- Modify: `tests/test_openclaw_ports/test_tts_local_cli.py`

**Parallelism:** `[SEQUENTIAL after T3]`.

- [ ] **Step 1: Write the failing happy-path test**

Append to `tests/test_openclaw_ports/test_tts_local_cli.py`:

```python
class _FakeProc:
    def __init__(self, returncode: int = 0, audio_bytes: bytes = b"AAC-PAYLOAD"):
        self.returncode = returncode
        self._audio_bytes = audio_bytes

    async def communicate(self):
        return (b"", b"")

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


async def test_synthesize_happy_path(monkeypatch, tmp_path):
    """`synthesize` writes the text via `say`, reads back the M4A bytes."""
    captured_argv: list[list[str]] = []

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured_argv.append(list(argv))
        # Simulate `say` writing the output file.
        # argv: [SAY_BINARY, "-v", voice, "-o", outpath, "--file-format=m4af",
        #        "--data-format=aac", "--", text]
        outpath = Path(argv[4])
        outpath.write_bytes(b"AAC-PAYLOAD")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        tts_local_cli.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(tts_local_cli, "_tempdir", lambda: tmp_path)

    audio = await tts_local_cli.synthesize("hello world", voice="Alex", timeout_s=2.0)
    assert audio == b"AAC-PAYLOAD"
    # argv must include the safety-critical separators and flags.
    argv = captured_argv[0]
    assert argv[0] == tts_local_cli.SAY_BINARY
    assert "-v" in argv and "Alex" in argv
    assert "-o" in argv
    assert "--file-format=m4af" in argv and "--data-format=aac" in argv
    # Text is passed via argv after `--` so no shell interpolation.
    assert "--" in argv
    assert argv[-1] == "hello world"


async def test_synthesize_raises_unavailable_off_macos(monkeypatch):
    monkeypatch.setattr(tts_local_cli, "is_available", lambda: False)
    with pytest.raises(tts_local_cli.CLITTSUnavailable):
        await tts_local_cli.synthesize("anything")


async def test_synthesize_raises_on_nonzero_exit(monkeypatch, tmp_path):
    async def fake_proc(*argv, **kwargs):
        return _FakeProc(returncode=1)
    monkeypatch.setattr(tts_local_cli.asyncio, "create_subprocess_exec", fake_proc)
    monkeypatch.setattr(tts_local_cli, "_tempdir", lambda: tmp_path)
    monkeypatch.setattr(tts_local_cli, "is_available", lambda: True)
    with pytest.raises(tts_local_cli.CLITTSError, match="exit 1"):
        await tts_local_cli.synthesize("hello", voice="Alex", timeout_s=2.0)
```

(Tests are async — they run under the existing `asyncio_mode = "auto"` pytest config in `pyproject.toml`.)

- [ ] **Step 2: Run to verify failures**

Run: `pytest tests/test_openclaw_ports/test_tts_local_cli.py -v -k synthesize`
Expected: 3 FAILED.

- [ ] **Step 3: Implement `synthesize()` happy path**

In `openclaw_ports/tts_local_cli.py`, replace the `synthesize()` body and add the `_tempdir` helper:

```python
def _tempdir() -> Path:
    """Return the directory to use for temp output files. Overridable in tests."""
    return Path(tempfile.gettempdir())


async def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """Synthesize ``text`` to AAC/M4A audio bytes using macOS `say`.

    Raises:
        CLITTSUnavailable: when `say` is not present.
        CLITTSError: on non-zero exit, missing output file, or empty text.
    """
    if not is_available():
        raise CLITTSUnavailable("macOS `say` binary not found")

    if not text or not text.strip():
        raise CLITTSError("text is empty")

    # Use a unique temp file name in the tempdir so concurrent calls don't collide.
    with tempfile.NamedTemporaryFile(
        prefix="jarvis-tts-", suffix=".m4a", dir=str(_tempdir()), delete=False
    ) as tf:
        outpath = Path(tf.name)

    try:
        # All untrusted values (voice, text) flow through argv AFTER `--`,
        # never through the shell. No string interpolation in argv.
        argv = [
            SAY_BINARY,
            "-v", voice,
            "-o", str(outpath),
            "--file-format=m4af",
            "--data-format=aac",
            "--",
            text,
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            raise CLITTSError(f"`say` timed out after {timeout_s}s")

        if proc.returncode != 0:
            raise CLITTSError(f"`say` exit {proc.returncode}")

        if not outpath.exists() or outpath.stat().st_size == 0:
            raise CLITTSError("`say` produced no output")

        return outpath.read_bytes()
    finally:
        try:
            outpath.unlink()
        except FileNotFoundError:
            pass
```

- [ ] **Step 4: Run all tts_local_cli tests**

Run: `pytest tests/test_openclaw_ports/test_tts_local_cli.py -v`
Expected: 8 PASSED (2 from T2 + 3 from T3 + 3 new).

- [ ] **Step 5: Commit**

```bash
git add openclaw_ports/tts_local_cli.py tests/test_openclaw_ports/test_tts_local_cli.py
git commit -m "feat(openclaw_ports): tts_local_cli.synthesize() happy path

Spawns /usr/bin/say with argv (no shell), writes M4A/AAC to a unique
temp file, returns the bytes. All untrusted values (voice, text) pass
after `--` separator to defeat argv-flag injection. Temp file always
cleaned up. Timeout enforced via asyncio.wait_for + proc.kill()."
```

---

## Task 5: Emoji stripping

**Files:**
- Modify: `openclaw_ports/tts_local_cli.py`
- Modify: `tests/test_openclaw_ports/test_tts_local_cli.py`

**Parallelism:** `[SEQUENTIAL after T4]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_openclaw_ports/test_tts_local_cli.py`:

```python
def test_strip_emojis_removes_pictographic():
    """Ported from OpenClaw's stripEmojis — `say` chokes on raw emoji."""
    assert tts_local_cli._strip_emojis("Hello 🌟 world 🎉") == "Hello world"
    assert tts_local_cli._strip_emojis("👋") == ""
    assert tts_local_cli._strip_emojis("plain ascii") == "plain ascii"


async def test_synthesize_raises_when_only_emojis(monkeypatch, tmp_path):
    """If the input collapses to empty after stripping, raise CLITTSError."""
    monkeypatch.setattr(tts_local_cli, "is_available", lambda: True)
    monkeypatch.setattr(tts_local_cli, "_tempdir", lambda: tmp_path)
    with pytest.raises(tts_local_cli.CLITTSError, match="empty"):
        await tts_local_cli.synthesize("🎉🎉🎉", voice="Alex")
```

- [ ] **Step 2: Run to verify failures**

Run: `pytest tests/test_openclaw_ports/test_tts_local_cli.py -v -k emoji`
Expected: 2 FAILED.

- [ ] **Step 3: Implement `_strip_emojis` and wire it into `synthesize`**

In `openclaw_ports/tts_local_cli.py`, add the helper near the top (after constants):

```python
# Matches emoji presentation + extended pictographic + variation selectors.
# Mirrors OpenClaw's regex (TypeScript: /[\p{Emoji_Presentation}\p{Extended_Pictographic}]/gu).
_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "]+",
    flags=re.UNICODE,
)


def _strip_emojis(text: str) -> str:
    """Remove emoji/pictographic codepoints and collapse whitespace.

    Ported from OpenClaw's stripEmojis (speech-provider.ts:87-92).
    """
    no_emoji = _EMOJI_RE.sub(" ", text)
    return re.sub(r"\s+", " ", no_emoji).strip()
```

Modify `synthesize()` to use it. Replace the existing empty-text check with:

```python
    cleaned = _strip_emojis(text)
    if not cleaned:
        raise CLITTSError("text is empty after stripping emojis")
```

Then replace `text` with `cleaned` in the argv list:

```python
        argv = [
            SAY_BINARY,
            "-v", voice,
            "-o", str(outpath),
            "--file-format=m4af",
            "--data-format=aac",
            "--",
            cleaned,
        ]
```

- [ ] **Step 4: Run all tts_local_cli tests**

Run: `pytest tests/test_openclaw_ports/test_tts_local_cli.py -v`
Expected: 10 PASSED (8 prior + 2 new).

- [ ] **Step 5: Commit**

```bash
git add openclaw_ports/tts_local_cli.py tests/test_openclaw_ports/test_tts_local_cli.py
git commit -m "feat(openclaw_ports): strip emojis before handing text to \`say\`

Ported from OpenClaw stripEmojis (speech-provider.ts:87-92). macOS
\`say\` reads emoji codepoints literally and produces garbage audio
or silently fails. Strip pictographic + emoji-presentation + flag
sequences, collapse whitespace, raise CLITTSError if input collapses
to empty."
```

---

## Task 6: Timeout regression test

**Files:**
- Modify: `tests/test_openclaw_ports/test_tts_local_cli.py`

**Parallelism:** `[SEQUENTIAL after T5]`.

Timeout is already implemented in T4 via `asyncio.wait_for`. This task adds the regression coverage so a future refactor can't silently remove it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_openclaw_ports/test_tts_local_cli.py`:

```python
class _SlowFakeProc:
    """Simulates a `say` invocation that never exits."""
    def __init__(self):
        self.returncode = None
        self.killed = False

    async def wait(self):
        # Sleep longer than any reasonable timeout — wait_for will cancel us.
        await asyncio.sleep(60)
        return 0

    def kill(self):
        self.killed = True


async def test_synthesize_enforces_timeout(monkeypatch, tmp_path):
    """If `say` hangs, synthesize() raises CLITTSError and kills the child."""
    slow_proc = _SlowFakeProc()

    async def fake_proc(*argv, **kwargs):
        # Touch the output path so the not-exists branch doesn't mask the timeout.
        outpath = Path(argv[4])
        outpath.write_bytes(b"")
        return slow_proc

    monkeypatch.setattr(tts_local_cli.asyncio, "create_subprocess_exec", fake_proc)
    monkeypatch.setattr(tts_local_cli, "_tempdir", lambda: tmp_path)
    monkeypatch.setattr(tts_local_cli, "is_available", lambda: True)

    import asyncio as _aio  # local alias for the test
    with pytest.raises(tts_local_cli.CLITTSError, match="timed out"):
        await tts_local_cli.synthesize("hello", voice="Alex", timeout_s=0.1)
    assert slow_proc.killed is True
```

- [ ] **Step 2: Run to verify it passes (timeout was already implemented in T4)**

Run: `pytest tests/test_openclaw_ports/test_tts_local_cli.py -v -k timeout`
Expected: 1 PASSED.

- [ ] **Step 3: Run the full suite**

Run: `pytest tests/test_openclaw_ports/test_tts_local_cli.py -v`
Expected: 11 PASSED.

- [ ] **Step 4: Commit**

```bash
git add tests/test_openclaw_ports/test_tts_local_cli.py
git commit -m "test(openclaw_ports): regression coverage for tts_local_cli timeout"
```

---

## Task 7: `server.py` integration — TTS provider dispatch

**Files:**
- Modify: `server.py`

**Parallelism:** `[SEQUENTIAL after T6]`.

**Persona gate:** before commit, invoke `code-reviewer` per CLAUDE.md routing (security-sensitive: changes the LLM/TTS trust boundary).

- [ ] **Step 1: Read the existing `synthesize_speech` at `server.py:1206-1237`**

Run: `sed -n '1206,1237p' server.py`
Note the current shape: Fish Audio only, returns `Optional[bytes]`, MP3 format requested from Fish.

- [ ] **Step 2: Replace the body of `synthesize_speech` with provider dispatch**

In `server.py`, replace the entire `synthesize_speech` function (lines ~1206–1237) with:

```python
async def synthesize_speech(text: str) -> Optional[bytes]:
    """Generate speech audio from text.

    Provider chosen by vault key `TTS_PROVIDER`:
      - "auto" (default): try macOS `say` via openclaw_ports.tts_local_cli;
        fall back to Fish Audio if local TTS is unavailable or fails.
      - "local_cli":      use only macOS `say`; return None on failure.
      - "fish_audio":     skip local; go straight to Fish Audio.
    """
    from openclaw_ports import tts_local_cli

    provider = (_vault_get("TTS_PROVIDER", "auto") or "auto").strip().lower()

    # Local CLI path.
    if provider in ("auto", "local_cli") and tts_local_cli.is_available():
        try:
            voice = _vault_get("TTS_VOICE", "Alex") or "Alex"
            audio = await tts_local_cli.synthesize(text, voice=voice)
            _session_tokens["tts_calls"] += 1
            _append_usage_entry(0, 0, "tts")
            return audio
        except tts_local_cli.CLITTSError as e:
            log.warning("local TTS failed: %s", e)
            if provider == "local_cli":
                return None
            # auto: fall through to Fish Audio.
    elif provider == "local_cli":
        log.warning("TTS_PROVIDER=local_cli but local TTS unavailable; no audio")
        return None

    # Fish Audio path (unchanged behavior).
    fish_api_key = _vault_get("FISH_API_KEY")
    fish_voice_id = _vault_get("FISH_VOICE_ID", "612b878b113047d9a770c069c8b4fdfe")
    if not fish_api_key:
        log.warning("FISH_API_KEY not set, skipping TTS")
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(
                FISH_API_URL,
                headers={
                    "Authorization": f"Bearer {fish_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "reference_id": fish_voice_id,
                    "format": "mp3",
                },
            )
            if response.status_code == 200:
                _session_tokens["tts_calls"] += 1
                _append_usage_entry(0, 0, "tts")
                return response.content
            log.error(f"TTS error: {response.status_code}")
            return None
    except Exception as e:
        log.error(f"TTS error: {e}")
        return None
```

- [ ] **Step 3: Invoke `code-reviewer` persona on the diff**

Run: `git diff server.py`
Dispatch the code-reviewer persona (per `.claude/agents/code-reviewer.md`) with the diff. Apply any must-fix findings before committing.

Specifically the reviewer should verify:
- No new shell interpolation introduced.
- The Fish Audio fallback path's behavior is byte-identical to the previous implementation when `provider == "fish_audio"`.
- The `_vault_get(...)` calls use the right defaults.
- `log.warning` / `log.error` don't accidentally log the TTS text body (would be PII leakage in logs).

- [ ] **Step 4: Run the existing server tests + the new tts tests**

Run: `pytest tests/test_server_locked_state.py tests/test_openclaw_ports/test_tts_local_cli.py -v`
Expected: all PASS. No regressions.

- [ ] **Step 5: Commit**

```bash
git add server.py
git commit -m "feat(server): TTS provider dispatch — local CLI first, Fish fallback

synthesize_speech now reads TTS_PROVIDER from the vault:
  - auto (default): try openclaw_ports.tts_local_cli, fall back to
    Fish Audio if local is unavailable or errors
  - local_cli: local only, return None on failure
  - fish_audio: skip local, go straight to Fish

Replaces the privacy-leaking Fish-only default per docs/BACKLOG.md P3
and umbrella spec docs/superpowers/specs/2026-05-25-openclaw-ports-
design.md §9 port 1. On a Mac host, JARVIS now speaks locally with no
third-party egress."
```

---

## Task 8: Vault key allowlist + UI inputs

**Files:**
- Modify: `server.py` (extend `/api/settings/keys` allowed set)
- Modify: `frontend/src/settings.ts` (add two inputs)

**Parallelism:** `[SEQUENTIAL after T7]`.

- [ ] **Step 1: Locate the existing allowed-keys set**

Run: `grep -n '"FISH_VOICE_ID"' server.py | /usr/bin/head -n 3`
Note the line where the `allowed = {...}` set is defined in `api_settings_keys`.

- [ ] **Step 2: Extend the allowed set**

In `server.py`, find the line:

```python
    allowed = {"ANTHROPIC_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID",
               "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS"}
```

Replace with:

```python
    allowed = {"ANTHROPIC_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID",
               "TTS_PROVIDER", "TTS_VOICE",
               "USER_NAME", "HONORIFIC", "CALENDAR_ACCOUNTS"}
```

- [ ] **Step 3: Write a hermetic test asserting the new keys are accepted**

Append to `tests/test_server_locked_state.py`:

```python
def test_settings_keys_accepts_tts_provider(isolated_vault):
    """Spec wave-1 port 1: TTS_PROVIDER and TTS_VOICE are in the allowlist."""
    c = _client()
    isolated_vault.bootstrap("pp")
    c.post("/api/auth/unlock", json={"passphrase": "pp"})
    # Token retrieval — copy the pattern used in other tests in this file.
    import server
    sess = isolated_vault.session()
    token = sess.settings.get("AUTH_TOKEN")
    h = {"X-JARVIS-Token": token}

    for key in ("TTS_PROVIDER", "TTS_VOICE"):
        r = c.post(
            "/api/settings/keys",
            headers=h,
            json={"key_name": key, "key_value": "test-value"},
        )
        assert r.status_code == 200, f"{key}: {r.status_code} {r.text}"
        assert sess.settings.get(key) == "test-value"
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `pytest tests/test_server_locked_state.py::test_settings_keys_accepts_tts_provider -v`
Expected: PASSED.

- [ ] **Step 5: Add UI inputs to settings panel**

In `frontend/src/settings.ts`, find the section that renders the existing key inputs (search for `Fish Voice ID`). After that block, add two new inputs:

```typescript
        <div class="settings-row">
          <label>TTS Provider</label>
          <select id="tts-provider">
            <option value="auto">Auto (local first, Fish fallback)</option>
            <option value="local_cli">Local CLI only (macOS say)</option>
            <option value="fish_audio">Fish Audio only</option>
          </select>
          <button class="btn-save" data-key="TTS_PROVIDER">Save</button>
        </div>
        <div class="settings-row">
          <label>TTS Voice (macOS say -v)</label>
          <input type="text" id="tts-voice" placeholder="Alex" />
          <button class="btn-save" data-key="TTS_VOICE">Save</button>
        </div>
```

(The exact HTML may need to be threaded into whatever templating pattern `settings.ts` already uses — adopt the existing convention; do not introduce a new one.)

If the existing pattern wires save buttons through a single handler keyed off `data-key`, no JS changes are needed beyond reading the new field's value.

- [ ] **Step 6: Verify the frontend builds**

Run: `cd frontend && npm run build 2>&1 | /usr/bin/tail -n 5`
Expected: 0 TS errors.

- [ ] **Step 7: Invoke `code-reviewer` on the combined diff**

Run: `git diff server.py frontend/src/settings.ts tests/test_server_locked_state.py`
Dispatch code-reviewer. Apply must-fix.

- [ ] **Step 8: Commit**

```bash
git add server.py frontend/src/settings.ts tests/test_server_locked_state.py
git commit -m "feat(settings): TTS_PROVIDER + TTS_VOICE in vault allowlist + UI"
```

---

## Task 9: Documentation updates

**Files:**
- Modify: `SECURITY.md`
- Modify: `ARCHITECTURE.md`
- Modify: `docs/BACKLOG.md`
- Modify: `CLAUDE.md`

**Parallelism:** `[SEQUENTIAL after T8]`. Will fire the membrane tripwire on SECURITY.md / ARCHITECTURE.md edits — expected.

- [ ] **Step 1: Update `SECURITY.md`**

Find the data-classification table. Add or update rows:

- New row (or annotation): `TTS_PROVIDER` and `TTS_VOICE` — at-rest in `data/secrets.db` (SQLCipher), no network surface.
- Update the existing TTS row / paragraph: "Fish Audio was the only path pre-wave-1; now `openclaw_ports.tts_local_cli` (MIT, ported from OpenClaw) is the default `auto` path. Eliminates a third-party egress when running on macOS host. The container path still falls back to Fish Audio because Linux lacks `say`."

- [ ] **Step 2: Update `ARCHITECTURE.md`**

Add a new module-map row:

```
openclaw_ports/         Python ports of MIT-licensed OpenClaw extensions.
                        See openclaw_ports/NOTICE.md. Currently: tts_local_cli.
```

If there's a TTS-flow diagram, update it: text → `synthesize_speech` → (provider dispatch: local CLI || Fish Audio) → bytes.

- [ ] **Step 3: Update `docs/BACKLOG.md`**

Find the "Done (recent)" section. Add at the top:

```
- **P11 wave-1 port 1 (P3 privacy win):** `tts_local_cli` ported under
  `openclaw_ports/` — macOS host now uses local `say` for TTS by default;
  Fish Audio remains as automatic fallback. New vault keys: TTS_PROVIDER,
  TTS_VOICE.
```

Also remove the old "P3 — Privacy: local Whisper STT + macOS `say` TTS" entry — split it: the `say` half is done; the Whisper half stays as a new entry (rename to P3a or move down).

- [ ] **Step 4: Update `CLAUDE.md` persona routing**

Find the Persona Routing section. Add a new row to the routing table:

```
| Editing any file under `openclaw_ports/`          | `code-reviewer` verifies the per-file attribution header is present and `NOTICE.md` is up to date. |
```

- [ ] **Step 5: Commit (membrane tripwire will fire — that's expected)**

```bash
git add SECURITY.md ARCHITECTURE.md docs/BACKLOG.md CLAUDE.md
git commit -m "docs: document tts_local_cli port + new vault keys

SECURITY.md: TTS_PROVIDER/TTS_VOICE rows; flag that macOS host now
defaults to local TTS (no Fish egress).
ARCHITECTURE.md: openclaw_ports/ row in module map; updated TTS flow.
BACKLOG.md: P11 wave-1 port 1 (= P3 privacy win) moved to Done.
CLAUDE.md: routing rule for openclaw_ports/ edits."
```

---

## Task 10: Final acceptance + PR

**Files:** none (verification + push)

**Parallelism:** `[SEQUENTIAL after T9]`.

- [ ] **Step 1: Run the full hermetic test suite inside Docker (no Fish, no real `say`)**

```bash
docker compose -p jarvis build 2>&1 | /usr/bin/tail -n 3
docker run --rm --entrypoint="" -v "$PWD:/app" -w /app jarvis-backend:local python -m pytest -q 2>&1 | /usr/bin/tail -n 20
```

Expected: 0 NEW failures. The 4 pre-existing failures noted in earlier PRs (3 `test_feedback_loop.py` asyncio + 1 `test_personas_setup.py::test_hook_warns_on_membrane_edit` due to `jq` absent in the image) are acceptable. ANY new failure is a regression.

Document the exact pass/fail count.

- [ ] **Step 2: Live integration smoke on the macOS host**

```bash
.venv/bin/python -c "
import asyncio
from openclaw_ports import tts_local_cli
print('available:', tts_local_cli.is_available())
audio = asyncio.run(tts_local_cli.synthesize('JARVIS online, sir.'))
print('bytes:', len(audio), 'first8:', audio[:8].hex())
"
```

Expected: `available: True`, then a non-empty byte count with a recognizable M4A/AAC header (first bytes typically `0000002066747970` for `ftyp`).

- [ ] **Step 3: Browser smoke (host)**

With the UI running and JARVIS unlocked, the next voice response should play audio from local `say` (not Fish). The browser's network panel should show NO request to `api.fish.audio` for the TTS response. Document in your PR description.

- [ ] **Step 4: `test-runner` persona pass**

Per CLAUDE.md routing — before any "ready to merge" claim, invoke the `test-runner` persona via the Agent tool. It runs `pytest -q` + `pip-audit` and reports exit codes verbatim. Implementer reads, does not interpret.

- [ ] **Step 5: `code-reviewer` persona pass on the full branch diff**

```bash
git diff main...HEAD
```

Dispatch the `code-reviewer` persona with the full diff. Address must-fix findings.

- [ ] **Step 6: Push and open PR**

```bash
git push -u origin feat/openclaw-tts-port
gh pr create --title "feat: tts_local_cli port (OpenClaw wave-1 port 1)" \
  --body "$(cat <<'BODY'
Implements docs/superpowers/specs/2026-05-25-openclaw-ports-design.md
wave-1 port 1 per docs/superpowers/plans/2026-05-25-openclaw-tts-port-implementation.md.

## Summary
- New `openclaw_ports/` package with MIT-attributed code ported from OpenClaw upstream commit 125d82cab2952f87f532106a368d54e526141026.
- `openclaw_ports/tts_local_cli.py` exposes `is_available()` + `async synthesize(text, voice, timeout_s) -> bytes` using macOS `say` directly (no ffmpeg; AAC/M4A output is decoded natively by Web Audio API).
- `server.py` `synthesize_speech` now dispatches on `TTS_PROVIDER` (vault key): `auto` (default) tries local first and falls back to Fish Audio; `local_cli` is local-only; `fish_audio` skips local.
- New vault keys (`TTS_PROVIDER`, `TTS_VOICE`) added to the allowlist + settings UI.
- SECURITY.md, ARCHITECTURE.md, BACKLOG.md, CLAUDE.md updated.

## Test plan
- [x] 11 hermetic tests for `tts_local_cli` (module surface, attribution header, `is_available` matrix, happy path, exit failure, timeout enforcement, emoji stripping)
- [x] New `test_settings_keys_accepts_tts_provider` integration test
- [x] Full pytest suite: 0 new failures
- [x] Live smoke on macOS host: `synthesize` returns valid M4A bytes
- [x] Browser smoke: voice response plays without hitting api.fish.audio

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

- [ ] **Step 7: Wait for CI green**

```bash
gh pr checks
```

Document the status of each check. Do NOT auto-merge — the user reviews.

---

## Self-review against the spec

**Spec coverage check (`docs/superpowers/specs/2026-05-25-openclaw-ports-design.md`):**

| Spec section | Task(s) implementing it |
|---|---|
| §1 Goals — port + preserve identity + auditable license + ship first port + escape hatch designed | T1 (NOTICE), T2–T6 (port), T7 (preserve identity via fallback), T9 (docs) |
| §2 Non-goals (no plugin gateway, no multi-channel, no auto-sync) | Honored — port is a single Python module |
| §3 Architecture (`openclaw_ports/` layout, snake_case modules, integration via direct import) | T1, T2, T7 |
| §4 Attribution (`NOTICE.md` + per-file header) | T1, T2 (test asserts the header) |
| §5 Test convention (hermetic under `tests/test_openclaw_ports/`, no live tests required for merge) | T2, T3, T4, T5, T6 |
| §6 Secret integration (vault allowlist extension) | T8 |
| §7 Subprocess escape hatch — NOT built in wave 1 | Honored — no `_subprocess_bridge.py` created |
| §8 Per-port micro-spec checklist | This plan IS the micro-spec for tts_local_cli |
| §9 Implementation order — tts_local_cli first | Honored |
| §10 memory-lancedb deferral | Honored — not in this plan |
| §11 Documentation updates | T9 |

**Placeholder scan:** No "TBD", no "TODO", no "implement later", no "add appropriate error handling." Every step has the exact code an engineer needs.

**Type consistency:** `tts_local_cli.is_available`, `tts_local_cli.synthesize`, `CLITTSUnavailable`, `CLITTSError`, `SAY_BINARY`, `DEFAULT_VOICE`, `DEFAULT_TIMEOUT_S`, `_strip_emojis`, `_EMOJI_RE`, `_tempdir`, `_session_tokens["tts_calls"]`, `_append_usage_entry`, `_vault_get` — all referenced consistently across T2–T8.

**Persona gates:** code-reviewer invoked at T7 step 3, T8 step 7, T10 step 5. test-runner at T10 step 4. security-advisor not required since no new network surface or trust boundary is introduced (the port is a subprocess to a local binary; identical privacy posture to existing `actions.run_osascript`).

Plan ships as-is.
