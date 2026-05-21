# Sandbox image for running ``claude -p`` under JARVIS.
#
# Build:
#     docker build -t jarvis-claude:latest docker/claude
#
# JARVIS launches one ephemeral container per spawn when
# JARVIS_CLAUDE_RUNNER=docker is set. The container reads the prompt
# from stdin, writes its response to stdout, and is destroyed
# (``--rm``). The only filesystem the container can see is the project
# directory mounted at /work plus the read-only Node + npm install
# baked into the image.
#
# Auth: Claude Code runs with the ANTHROPIC_API_KEY env var passed in
# at ``docker run`` time. There is no persistent ~/.claude inside the
# container; nothing the LLM can do can read your local Claude
# subscription tokens.

FROM node:22-alpine

# Non-root user. The container has no need for root privileges; running
# as a regular user is defense in depth against container-escape bugs.
RUN addgroup -g 1000 jarvis \
 && adduser -D -u 1000 -G jarvis jarvis

# Install Claude Code CLI globally. Pin a known-good version so the
# image is reproducible; bump deliberately, never opportunistically.
ARG CLAUDE_CODE_VERSION=1.0.69
RUN npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force

# /work is where JARVIS mounts the project directory. Created with
# jarvis ownership so the entrypoint can write into a bind-mounted
# directory owned by uid 1000 on the host. On macOS, Docker Desktop
# usually maps the host user → uid 1000 inside the container, so a
# Desktop project directory shows up writable. If you see permission
# errors at runtime, run the daemon with --user "$(id -u):$(id -g)".
RUN mkdir -p /work && chown -R jarvis:jarvis /work

USER jarvis
WORKDIR /work

# Default cwd. JARVIS overrides with -w /work but having a sensible
# default makes ``docker run -it jarvis-claude:latest bash`` work too.
ENV HOME=/home/jarvis

# Entrypoint: claude -p reads prompt from stdin, writes to stdout. The
# ``--dangerously-skip-permissions`` flag is what makes JARVIS's
# orchestration work in the first place — it has the same semantics
# inside the container, but now the blast radius is bounded by the
# mounted volume rather than the entire host filesystem.
ENTRYPOINT ["claude", "-p", "--output-format", "text", "--dangerously-skip-permissions"]
