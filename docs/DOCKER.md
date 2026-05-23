# Running JARVIS in Docker

Local container setup for the JARVIS backend. This is the LLM/voice/memory
side only — macOS integrations (Calendar, Mail, Notes, Terminal, Chrome)
require AppleScript and do **not** work inside a Linux container.

## What you get

- Backend on `127.0.0.1:8340` (loopback only, never LAN).
- Same FastAPI server, WebSocket protocol, and auth-token model as
  `python server.py` on the host.
- Resource caps: 1 GiB memory, 1 CPU. Process runs as non-root (uid 10001).
- All Linux capabilities dropped; `no-new-privileges` set.
- Persistent state (`data/`) lives on the host via a bind mount; everything
  else inside the container is ephemeral.

## What you DON'T get

| Feature | Why |
|---|---|
| Calendar / Mail / Notes read | All use `osascript`, host-only. |
| `[ACTION:BUILD]` opening Terminal.app | AppleScript-driven. |
| `[ACTION:BROWSE]` opening Chrome | AppleScript-driven. |
| `[ACTION:RESEARCH]` headless browsing | Playwright deliberately omitted from the image (see below). |
| Spawned `claude -p` sessions | No `claude` CLI in the image. Use the existing host-side `JARVIS_CLAUDE_RUNNER=docker` sandbox at `docker/claude/` instead. |

The voice loop, LLM responses, memory, audit log, tasks, dispatch tracking,
and conversation state all work normally.

## Egress allowlist (the safety story)

The image only ever talks to two hosts:

| Host | Why | Where it's called from |
|---|---|---|
| `api.anthropic.com` | LLM (Claude) | `anthropic` SDK |
| `api.fish.audio` | Text-to-speech | raw `httpx` POST in `server.py` |

No telemetry, no auto-update, no metrics endpoint. Audited deps:

| Package | Pinned | Phones home? |
|---|---|---|
| anthropic | 0.39.0 | Only `api.anthropic.com` (the LLM itself). |
| httpx | 0.27.2 | No. |
| fastapi / starlette | 0.136.1 / 0.49.1 | No. |
| uvicorn[standard] | 0.32.1 | No. |
| pydantic | 2.10.3 | No. |
| websockets | 13.1 | No. |
| pyyaml | 6.0.2 | No. |
| playwright | 1.49.0 | **Omitted from the image** — bundles Chromium and has update-check channels we don't need for the voice loop. Use host-side if you need `[ACTION:RESEARCH]`. |

Defense-in-depth env vars set in compose: `DO_NOT_TRACK=1`,
`PIP_DISABLE_PIP_VERSION_CHECK=1`, `ANTHROPIC_LOG_LEVEL=warning`.

### Want a hard network gate?

Compose alone doesn't restrict egress — the bridge network can reach the
internet. If you want a kernel-enforced allowlist:

```yaml
# Optional sidecar pattern, NOT included by default. Add a tinyproxy
# (or your egress proxy of choice) with an allowlist for api.anthropic.com
# and api.fish.audio, set HTTPS_PROXY in the backend service, and drop
# the backend's outbound default route.
```

A future PR can ship this as `docker-compose.locked.yml` once we agree on
a proxy. For now, the protection is dependency-level: nothing in
`requirements.txt` reaches out without an explicit code path that you can
audit.

## Setup

1. Build:
   ```bash
   docker compose build
   ```
2. Make sure your `.env` exists in the repo root (same one you use on the
   host). Required keys: `ANTHROPIC_API_KEY`, `FISH_API_KEY`. The compose
   file pulls env from `.env` directly — secrets do not enter the image.
3. Start:
   ```bash
   docker compose up -d
   ```
4. Check health:
   ```bash
   curl -s http://127.0.0.1:8340/api/health
   # {"status":"online","name":"JARVIS","version":"0.1.0"}
   ```
5. Tail logs:
   ```bash
   docker compose logs -f backend
   ```

## Coexistence with other Docker stacks

This compose file uses `name: jarvis`. Every resource (container, network,
volume) is prefixed with `jarvis_` or `jarvis-`. Other stacks running on
the same Docker daemon (e.g. `magistrel-*`) are unaffected.

`docker compose -p jarvis down` shuts down JARVIS only. **Never** run
`docker system prune -a` without checking what else is on the host.

The custom bridge network `jarvis_net` is isolated — JARVIS cannot reach
your other compose networks unless you explicitly attach it.

## Frontend?

Not included. The Vite dev server (`cd frontend && npm run dev`) is a
front-of-house tool; run it on the host and point your browser at the
backend on `127.0.0.1:8340`. We can add a `frontend` service later if you
want to ship a built-and-served bundle, but that requires baking the
backend URL at build time.

## Stopping

```bash
docker compose -p jarvis down       # stops + removes JARVIS only
docker compose -p jarvis down -v    # ALSO removes the named volumes (none today, but future-proofing)
```

Your `data/` directory on the host is **not** deleted by `down -v` because
it's a bind mount, not a Docker-managed volume.
