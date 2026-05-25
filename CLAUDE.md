# JARVIS — Voice AI Assistant

## Overview
JARVIS (Just A Rather Very Intelligent System) is a voice-first AI assistant for macOS. It runs locally on your machine, connecting to your Apple Calendar, Mail, Notes, and can spawn Claude Code sessions for development tasks.

## Quick Start
When a user clones this repo and starts Claude Code, help them:
1. Copy .env.example to .env
2. Get an Anthropic API key from console.anthropic.com
3. Get a Fish Audio API key from fish.audio
4. Install Python dependencies: pip install -r requirements.txt
5. Install frontend dependencies: cd frontend && npm install
6. Generate SSL certs: openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'
7. Run the backend: python server.py
8. Run the frontend: cd frontend && npm run dev
9. Open Chrome to http://localhost:5173
10. Click to enable audio, speak to JARVIS

## Architecture
- **Backend**: FastAPI + Python (server.py, ~2300 lines)
- **Frontend**: Vite + TypeScript + Three.js (audio-reactive orb)
- **Communication**: WebSocket (JSON messages + binary audio)
- **AI**: Claude Haiku for fast responses, Claude Opus for research
- **TTS**: Fish Audio with JARVIS voice model
- **System**: AppleScript for Calendar, Mail, Notes, Terminal integration

## Key Files
- `server.py` — Main server, WebSocket handler, LLM integration, action system
- `frontend/src/orb.ts` — Three.js particle orb visualization
- `frontend/src/voice.ts` — Web Speech API + audio playback
- `frontend/src/main.ts` — Frontend state machine
- `memory.py` — SQLite memory system with FTS5 search
- `calendar_access.py` — Apple Calendar integration via AppleScript
- `mail_access.py` — Apple Mail integration (READ-ONLY)
- `notes_access.py` — Apple Notes integration
- `actions.py` — System actions (Terminal, Chrome, Claude Code)
- `browser.py` — Playwright web automation
- `work_mode.py` — Persistent Claude Code sessions

## Environment Variables
- `ANTHROPIC_API_KEY` (required) — Claude API access
- `FISH_API_KEY` (required) — Fish Audio TTS
- `FISH_VOICE_ID` (optional) — Voice model ID
- `USER_NAME` (optional) — Your name for JARVIS to use
- `CALENDAR_ACCOUNTS` (optional) — Comma-separated calendar emails

## Conventions
- JARVIS personality: British butler, dry wit, economy of language
- Max 1-2 sentences per voice response
- Action tags: [ACTION:BUILD], [ACTION:BROWSE], [ACTION:RESEARCH], etc.
- AppleScript for all macOS integrations (no OAuth needed)
- Read-only for Mail (safety by design)
- SQLite for all local data storage

## Persona Routing

Five project-specific personas live in `.claude/agents/`. Use them per this table. The architecture is documented in `docs/superpowers/specs/2026-05-21-personas-design.md`.

| Task pattern | Routing |
|---|---|
| Editing `auth.py`, `untrusted_content.py`, `claude_pool.py`, `claude_runner.py`, `cwd_allowlist.py`, `audit_log.py`, `file_perms.py` | Invoke `security-advisor` BEFORE the edit. Apply its findings. |
| Editing `server.py` lines that match `osascript`, `claude -p`, `subprocess`, `JARVIS_SYSTEM_PROMPT`, `extract_action`, or any `*_access.py` AppleScript builder | `security-advisor` first. |
| New module, removing a module, changing trust boundaries, refactoring across 3+ files | `software-architect` first. |
| Before committing any change with ≥30 LOC diff, or any change that touched the security-sensitive list above | `code-reviewer`. |
| Before claiming "tests pass," "ready to merge," or creating a PR | `test-runner` (separate identity, no synthesis). |
| Task spans multiple categories OR is unclear which persona owns it OR the user asks for "a full review" | `controller`. It picks and sequences. |
| Editing any file under `openclaw_ports/` | `code-reviewer` verifies the per-file attribution header is present and `NOTICE.md` is up to date. |
| Routine work (docs typos, README edits, comment-only changes) | No persona. Proceed directly. |

### Principles

1. **Read-only personas, main session applies.** Personas produce reports. The main session reads them and Edits.
2. **Pre-commit gates are mandatory.** `code-reviewer` + `test-runner` before every PR. Branch protection enforces CI; this adds the human-judgment layer.
3. **The controller is for ambiguity, not bypass.** If the table says `security-advisor` first, the controller honors that.

### Membrane (tripwire only)

These three files trigger a PostToolUse advisory warning on Edit:
- `SECURITY.md`
- `ARCHITECTURE.md`
- `auth.py`

The warning is a tripwire, not a block. The hard gate is branch protection + CI + the code-reviewer persona in this routing.

### Cost discipline

Opus calls are not free. The rule is "invoke before the edit," but the advisor's output gets cached *in the session*: for follow-ups in the same session, the main session reuses the cached judgment unless something material changed.
