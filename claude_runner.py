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


_VALID_BACKENDS = ("direct", "docker", "sidecar")
_IMAGE_TAG = "jarvis-claude:latest"


def _running_in_docker() -> bool:
    """Best-effort detection that we're inside a Linux container."""
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(errors="ignore")
        return "docker" in cgroup or "containerd" in cgroup
    except (OSError, FileNotFoundError):
        return False


def _detect_default_backend() -> str:
    """Auto-detect a sensible default when JARVIS_CLAUDE_RUNNER is unset.

    In Docker, the container has no `claude` CLI; fall through to the
    host sidecar's /spawn endpoint (see spec 2026-05-29). On a native
    host install, the historical `direct` backend stays the default.
    """
    if _running_in_docker():
        return "sidecar"
    return "direct"


def _read_backend() -> str:
    env = os.getenv("JARVIS_CLAUDE_RUNNER")
    raw = (env or _detect_default_backend()).strip().lower()
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


async def _run_via_sidecar(
    *,
    prompt: bytes,
    cwd: str | Path,
    timeout: float,
) -> tuple[int, bytes, bytes]:
    """Run claude on the macOS host via the sidecar `/spawn` endpoint.

    Used when JARVIS lives in Docker (no `claude` CLI in the container).
    POSTs `/spawn`, polls `/spawn/{id}` until status terminal, returns
    ``(exit_code, output_bytes, b"")``. The sidecar merges stderr into
    stdout, so stderr from this backend is always empty.

    Raises ``asyncio.TimeoutError`` if the sidecar doesn't return a
    terminal status within ``timeout + 60`` seconds (matches the call
    contract of the other backends).
    """
    import sidecar_client as _sc
    workdir = str(Path(cwd).expanduser().resolve())
    prompt_text = prompt.decode("utf-8", errors="replace")

    res = await _sc.spawn_via_sidecar(
        prompt_text, workdir, timeout_s=float(timeout),
    )
    if res is None:
        # Spawn rejected (auth/validation/admission). Treat as fail.
        log.warning("claude_runner: sidecar /spawn rejected the request")
        return 1, b"", b"sidecar /spawn rejected"
    session_id = res.get("session_id")
    if not session_id:
        return 1, b"", b"sidecar /spawn returned no session_id"

    deadline = asyncio.get_event_loop().time() + timeout + 60.0
    last_status: dict | None = None
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.0)
        last_status = await _sc.spawn_status(session_id)
        if last_status is None:
            # Could be transient — keep trying until deadline.
            continue
        if last_status.get("status") != "running":
            output = (last_status.get("output") or "").encode("utf-8")
            exit_code = last_status.get("exit_code") or 0
            return exit_code, output, b""

    # Timed out polling — try to kill, then raise to mirror the other backends.
    log.warning("claude_runner: sidecar poll deadline exceeded; killing session %s", session_id)
    await _sc.spawn_kill(session_id)
    raise asyncio.TimeoutError("sidecar /spawn poll deadline exceeded")


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
    if BACKEND == "sidecar":
        if extra_flags:
            log.warning(
                "claude_runner: sidecar backend ignores extra_flags=%r — the "
                "sidecar enforces the argv allowlist",
                extra_flags,
            )
        log.info("claude_runner: backend=sidecar cwd=%s", cwd)
        return await _run_via_sidecar(prompt=prompt, cwd=cwd, timeout=timeout)

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
