# JARVIS — Security Model

## Threat model in one sentence
JARVIS is a single-user, single-host voice assistant. The trust boundary
is the local machine: anything that can reach the listening socket can
drive the assistant, and the assistant can read Calendar/Mail/Notes and
spawn Claude Code sessions with full shell access. Network exposure is
disabled by default; opting in requires presenting an auth token.

## Defaults
- **Bind:** `127.0.0.1` (loopback only). Network exposure requires
  `--host 0.0.0.0` (or any other interface) explicitly.
- **Auth:** Loopback requests bypass the token (single-user case).
  Non-loopback requests must present `X-JARVIS-Token` header or
  `?token=...` query param. Token is generated on first start and
  persisted to `data/.local_token` with mode 0600.
- **CORS:** Allowlist only (`http://localhost:5173` and `http://127.0.0.1:5173`
  by default). Override with `JARVIS_CORS_ORIGINS` (comma-separated).
- **`/api/fix-self`:** Disabled unless `JARVIS_ENABLE_FIX_SELF=1` is set,
  and the request body must include `{"confirm": "rewrite-self"}`. The
  endpoint spawns a Claude Code session with `--dangerously-skip-permissions`;
  treat as full local code execution.

## Data classification

| Data                                | Class       | At rest          | In transit          |
|-------------------------------------|-------------|------------------|---------------------|
| `ANTHROPIC_API_KEY`, `FISH_API_KEY` | Secret      | `.env` (gitignored) | TLS to provider     |
| `data/.local_token`                 | Secret      | `data/`, mode 0600 | header/query        |
| Calendar / Mail / Notes content     | PII         | OS apps           | osascript stdout    |
| Memory database (`*.db`)            | PII         | local SQLite      | n/a                 |
| Cost telemetry (`data/usage.jsonl`) | Internal    | local             | n/a                 |
| Session token counters              | Internal    | in-memory         | `/api/usage` (auth) |

## What is intentionally NOT defended against
- A user with a shell on the JARVIS host. The server runs as that user
  and can do anything they can do.
- A user who explicitly sets `--host 0.0.0.0` and shares the token, or
  sets `JARVIS_TRUST_LOOPBACK=0` on a multi-user machine. Those are
  affirmative choices.
- Compromise of Apple Calendar/Mail/Notes themselves — read paths are
  read-only by design; write paths are limited to Notes creation.

## AppleScript injection
All AppleScript invocations that interpolate runtime values pass those
values via `osascript` argv (`item N of argv` inside `on run argv`),
never via f-string interpolation into the script source. The shell-exec
primitive (`do script`) is reachable only through call sites whose
inputs are either literal constants or regex-restricted (see
`_assert_safe_path` in `actions.py`).

## Reporting
This is a personal project. For coordinated disclosure of issues that
could affect anyone running the public repo, open a private issue or
email the repo owner. Do not file public issues for unpatched
vulnerabilities.

## Operator's checklist before exposing on LAN
1. Run with `--host 0.0.0.0` (or specific interface).
2. Capture the token printed at startup (also at `data/.local_token`).
3. Configure the remote client to send `X-JARVIS-Token: <token>`
   on REST and `?token=<token>` on the WebSocket URL.
4. Restrict `JARVIS_CORS_ORIGINS` to the remote frontend origin only.
5. Consider whether `/api/fix-self` should be enabled (default: no).
6. Prefer HTTPS — drop `cert.pem`/`key.pem` next to `server.py` and
   the server auto-enables TLS.
