# JARVIS backend — containerized (LLM + voice loop + memory only).
#
# What works inside the container:
#   - FastAPI server, WebSocket voice protocol
#   - Anthropic LLM calls (api.anthropic.com)
#   - Fish Audio TTS calls (api.fish.audio)
#   - SQLite memory + audit log
#   - Conversation, task, dispatch tracking
#
# What does NOT work (macOS host-only, requires AppleScript):
#   - Apple Calendar / Mail / Notes read
#   - Terminal.app launching, Chrome.app launching
#   - claude -p spawn (no claude CLI in image; use JARVIS_CLAUDE_RUNNER=docker
#     on the host instead — see docker/claude/README.md)
#   - Playwright browser automation (intentionally omitted to keep egress tight)
#
# Egress: this image legitimately talks to exactly two hosts —
#   api.anthropic.com  (LLM)
#   api.fish.audio     (TTS)
# Nothing else. See docs/DOCKER.md for the audited dep list.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DO_NOT_TRACK=1

# Minimal OS packages. No build tools at runtime; only what wheels need.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        libsqlcipher-dev \
        libsqlcipher1 \
        build-essential \
        python3-dev \
 && rm -rf /var/lib/apt/lists/*

# Non-root user (uid 10001 — outside the typical host-uid range)
RUN groupadd --system --gid 10001 jarvis \
 && useradd  --system --uid 10001 --gid jarvis --home /app --shell /usr/sbin/nologin jarvis

WORKDIR /app

# Install only the requirements actually used at runtime.
# Playwright is in requirements.txt but is NOT installed here on purpose:
# it would bring in Chromium + potential telemetry channels we don't need
# for the LLM/voice path. If you ever want browser.py inside the container,
# build a separate `Dockerfile.browser` image; do not bundle it here.
COPY requirements.txt /tmp/requirements.txt
RUN grep -v -E '^playwright(==|$)' /tmp/requirements.txt > /tmp/requirements.docker.txt \
 && pip install --no-cache-dir -r /tmp/requirements.docker.txt \
 && rm /tmp/requirements*.txt

# Copy only the runtime surface. .dockerignore keeps secrets out.
COPY --chown=jarvis:jarvis . /app

# Pre-create writable dirs as the jarvis user.
RUN mkdir -p /app/data /app/data/audit \
 && chown -R jarvis:jarvis /app/data

USER jarvis

# Default port matches server.py's CLI default (8340).
EXPOSE 8340

# tini reaps zombies and forwards signals cleanly to uvicorn.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Loopback bind by default — compose maps 127.0.0.1:8340 on the host.
# Override with --host 0.0.0.0 ONLY if you understand the LAN exposure
# implications described in SECURITY.md.
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8340"]
