# JARVIS — Architecture

## One-paragraph summary
A FastAPI server (`server.py`) runs locally on macOS and exposes a
WebSocket (`/ws/voice`) plus a small REST surface (`/api/*`). The
browser frontend (Vite + TS, in `frontend/`) captures speech via Web
Speech API, streams transcripts over the WebSocket, and plays back
Fish Audio TTS audio frames. The server routes transcripts through
Claude (Haiku for fast turns, Opus for research), classifies intent,
and either replies in voice or invokes an action: open Terminal, open
a browser, spawn a Claude Code subprocess, read Calendar/Mail/Notes,
etc. All macOS integrations are AppleScript via `osascript`.

## Trust boundaries
1. **Network → server.** Loopback bypasses auth; non-loopback requires
   `X-JARVIS-Token`. CORS allowlisted to known frontend origins.
2. **Server → LLM (Anthropic).** TLS; API key from env.
3. **Server → Fish Audio.** TLS; API key from env.
4. **Server → macOS apps.** `osascript` argv-only (no source
   interpolation); shell-exec call sites validated by `_assert_safe_path`.
5. **Server → Claude Code subprocess.** Spawned via `claude -p`. Inherits
   user's permissions. Triple-gated for the self-modify path.

## Module map

| File                  | Role                                             |
|-----------------------|--------------------------------------------------|
| `server.py`           | FastAPI app, WS handler, LLM glue, REST endpoints |
| `auth.py`             | Local-token auth middleware + WS gate            |
| `actions.py`          | Terminal/Browser/Claude-Code launchers           |
| `calendar_access.py`  | Apple Calendar bulk read via AppleScript         |
| `mail_access.py`      | Apple Mail read (no send/delete by design)       |
| `notes_access.py`     | Apple Notes read + create (no edit/delete)       |
| `memory.py`           | SQLite + FTS5 long-term memory                   |
| `conversation.py`     | Three-tier conversation memory                   |
| `planner.py`          | Multi-step task planning                         |
| `screen.py`           | Active windows + screenshot via macOS APIs       |
| `browser.py`          | Playwright web automation                        |
| `work_mode.py`        | Persistent Claude Code session state             |
| `dispatch_registry.py`| Intent dispatch table                            |
| `ab_testing.py`       | Response variant experimentation                 |
| `evolution.py`        | Self-tuning of prompts/heuristics                |
| `learning.py`         | Feedback loop persistence                        |
| `monitor.py`          | Background loop / build watchers                 |
| `qa.py`               | LLM-as-judge for response quality                |
| `suggestions.py`      | Proactive nudge generator                        |
| `templates.py`        | Response templates                               |
| `tracking.py`         | Per-event usage tracking                         |

## Voice → response sequence (happy path)
1. Browser captures speech, sends `{"type":"transcript","text":...,"isFinal":true}`.
2. `server.voice_handler` builds context: memory recall, calendar
   summary, mail digest, last response.
3. Anthropic Haiku is called with a system prompt and the rolling
   conversation buffer. The response may contain `[ACTION:X]` tags.
4. If an action tag is present, `actions.execute_action` dispatches it
   (e.g. `open_terminal`, `open_browser`, `open_claude_in_project`).
5. The textual reply is sent to Fish Audio for TTS; the resulting MP3
   bytes are base64-encoded and shipped over the WS as `{"type":"audio"}`.
6. Conversation buffer is rolled forward; old messages summarized.

## Persistence

| Path                       | Purpose                                       |
|----------------------------|-----------------------------------------------|
| `.env`                     | API keys (gitignored)                         |
| `data/.local_token`        | Auth token (gitignored, mode 0600)            |
| `data/*.jsonl`             | Usage telemetry, session history (gitignored) |
| `*.db`                     | SQLite memory (gitignored)                    |

## Not yet documented (drift to close)
- The exact dispatch table inside `dispatch_registry.py`.
- The action-tag taxonomy (`[ACTION:BUILD]`, `[ACTION:BROWSE]`,
  `[ACTION:RESEARCH]`, ...) — currently described in `CLAUDE.md`,
  should be canonical here.
- The frontend↔backend WebSocket protocol — currently a docstring
  on `voice_handler`.

If you change any boundary above (especially #1, #4, or #5), update
this file in the same PR.
