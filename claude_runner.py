"""
Unified runner for ``claude -p`` subprocess spawns.

Why this module exists:
    Five sites in the codebase need to launch Claude Code with a
    prompt on stdin and capture stdout. Before this module each one
    inlined its own ``asyncio.create_subprocess_exec("claude", "-p",
    ...)`` call. That worked when the only mode was "fork on the
    host", but now we want a second mode where each spawn runs
    inside an ephemeral Docker container, and we don't want to
    duplicate that switch in five places.

Backends:
    ``direct``
        ``claude -p --output-format text --dangerously-skip-permissions``
        executed on the host with ``cwd=path``. Same behavior the
        codebase has always had. Default.

    ``docker``
        ``docker run --rm -i --memory=2g --cpus=1 -v {path}:/work
        -e ANTHROPIC_API_KEY -w /work jarvis-claude:latest`` with
        the prompt on stdin. The container is built from
        ``docker/claude/Dockerfile`` and tagged ``jarvis-claude:latest``.
        Provides kernel-level filesystem isolation: the LLM can only
        see the project directory, not the rest of the host.

Backend selection:
    Env var ``JARVIS_CLAUDE_RUNNER`` — ``direct`` (default) or
    ``docker``. Invalid values fall back to ``direct`` with a
    warning. The choice is made per-process at module import time;
    changing the env var requires restarting the server.

What this module does NOT do:
    * Acquire the ``claude_pool`` slot. Callers do that around the
      ``run(...)`` call (some callers want immediate-fail semantics,
      others want queue-blocking; the runner shouldn't decide).
    * Check the cwd allowlist. Callers do that too — the failure
      mode for an out-of-allowlist cwd is highly site-specific
      (return a QAResult vs. a string vs. set task.status).
    * Audit-log. Same reasoning.

    The runner is a thin shim. The security envelope around it is
    composed at each call site from claude_pool + cwd_allowlist +
    audit_log + the runner's own backend choice.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("jarvis.claude_runner")


_VALID_BACKENDS = ("direct", "docker")
_IMAGE_TAG = "jarvis-claude:latest"


def _read_backend() -> str:
    raw = (os.getenv("JARVIS_CLAUDE_RUNNER", "direct") or "direct").strip().lower()
    if raw not in _VALID_BACKENDS:
        log.warning(
            "JARVIS_CLAUDE_RUNNER=%r is not one of %s; defaulting to 'direct'",
            raw, _VALID_BACKENDS,
        )
        return "direct"
    return raw


BACKEND = _read_backend()


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _build_direct_cmd(extra_flags: list[str] | None = None) -> list[str]:
    cmd = [
        "claude", "-p",
        "--output-format", "text",
        "--dangerously-skip-permissions",
    ]
    if extra_flags:
        cmd.extend(extra_flags)
    return cmd


def _build_docker_cmd(cwd: str | Path, extra_flags: list[str] | None = None) -> list[str]:
    """Compose the ``docker run`` argv for a sandboxed spawn.

    The image's ENTRYPOINT already contains ``claude -p ...``. We
    pass through extra flags (e.g. ``--continue``) by appending them
    to the docker run argv after the image name.
    """
    cwd_abs = str(Path(cwd).expanduser().resolve())
    # The ANTHROPIC_API_KEY is read from the host env at docker-run
    # time and forwarded into the container only. We don't bake it
    # into the image or write it to disk.
    docker_cmd = [
        "docker", "run", "--rm", "-i",
        "--memory=2g",
        "--cpus=1",
        "-v", f"{cwd_abs}:/work:rw",
        "-w", "/work",
        "-e", "ANTHROPIC_API_KEY",
        _IMAGE_TAG,
    ]
    if extra_flags:
        docker_cmd.extend(extra_flags)
    return docker_cmd


async def run(
    *,
    prompt: bytes,
    cwd: str | Path,
    timeout: float,
    extra_flags: list[str] | None = None,
) -> tuple[int, bytes, bytes]:
    """Run claude -p once. Returns ``(returncode, stdout, stderr)``.

    Selects backend by the module-level ``BACKEND`` constant. Raises
    ``asyncio.TimeoutError`` if the subprocess doesn't finish in
    ``timeout`` seconds — the caller is responsible for catching
    and reporting that as it sees fit.

    ``prompt`` is sent on stdin so the prompt content never appears
    in the argv (no leakage to ``ps``, no length limit).
    """
    if BACKEND == "docker":
        if not _docker_available():
            log.warning(
                "JARVIS_CLAUDE_RUNNER=docker but `docker` not on PATH; falling back to direct"
            )
            argv = _build_direct_cmd(extra_flags)
            spawn_cwd: str | Path | None = cwd
        else:
            argv = _build_docker_cmd(cwd, extra_flags)
            # ``cwd`` for the docker process itself doesn't matter —
            # the container sees /work, not the host cwd. Let docker
            # inherit our cwd (cheap, no syscall).
            spawn_cwd = None
    else:
        argv = _build_direct_cmd(extra_flags)
        spawn_cwd = cwd

    log.info("claude_runner: backend=%s argv[0:2]=%s cwd=%s", BACKEND, argv[:2], cwd)

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=spawn_cwd,
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(input=prompt),
        timeout=timeout,
    )
    return process.returncode or 0, stdout, stderr
