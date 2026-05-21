# jarvis-claude — sandbox image for `claude -p`

When `JARVIS_CLAUDE_RUNNER=docker` is set, JARVIS launches one
ephemeral container per `claude -p` spawn instead of running the
CLI directly on the host. The container has:

- Only `/work` mounted from the host (the project directory the LLM
  is operating on); the rest of your filesystem is invisible.
- 2 GiB memory cap, 1 CPU.
- Non-root user inside the container.
- No persistent state (`--rm`).
- Outbound network allowed (Claude Code talks to api.anthropic.com).

This complements `cwd_allowlist`: the allowlist is a logical fence,
the container is a kernel-level one.

## Setup

Install Docker Desktop or OrbStack on macOS, then:

```bash
docker build -t jarvis-claude:latest docker/claude
```

Then run JARVIS with the sandbox enabled:

```bash
JARVIS_CLAUDE_RUNNER=docker python server.py
```

## What it costs you

- ~1–2 sec extra per spawn (container startup).
- ~500 MB image on disk (one-time).
- `ANTHROPIC_API_KEY` env auth only — no Claude Code subscription
  features (login session stays on the host). If you need the
  subscription, leave the runner in `direct` mode.

## Bumping Claude Code

Update the `CLAUDE_CODE_VERSION` build arg in `Dockerfile` and
rebuild. Don't track `latest` — pin so the image is reproducible.
