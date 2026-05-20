"""
Tests for claude_runner — backend selection + argv construction.

Doesn't actually invoke ``claude`` or ``docker``. Verifies the shim
constructs the right argv shape for each backend so a wiring
regression is caught by CI rather than at runtime.
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reload_runner(backend_env: str | None = None):
    if backend_env is None:
        os.environ.pop("JARVIS_CLAUDE_RUNNER", None)
    else:
        os.environ["JARVIS_CLAUDE_RUNNER"] = backend_env
    if "claude_runner" in sys.modules:
        del sys.modules["claude_runner"]
    import claude_runner as cr  # noqa: WPS433
    return cr


def test_default_backend_is_direct():
    cr = _reload_runner(None)
    assert cr.BACKEND == "direct"


def test_explicit_direct_backend():
    cr = _reload_runner("direct")
    assert cr.BACKEND == "direct"


def test_explicit_docker_backend():
    cr = _reload_runner("docker")
    assert cr.BACKEND == "docker"


def test_invalid_backend_falls_back_to_direct():
    cr = _reload_runner("kubernetes")
    assert cr.BACKEND == "direct"


def test_blank_backend_falls_back_to_direct():
    cr = _reload_runner("")
    assert cr.BACKEND == "direct"


def test_case_insensitive():
    cr = _reload_runner("DOCKER")
    assert cr.BACKEND == "docker"


def test_build_direct_cmd_shape():
    cr = _reload_runner("direct")
    cmd = cr._build_direct_cmd()
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--output-format" in cmd
    assert "text" in cmd


def test_build_direct_cmd_with_extra_flags():
    cr = _reload_runner("direct")
    cmd = cr._build_direct_cmd(extra_flags=["--continue"])
    assert "--continue" in cmd
    # Extra flag must come after the base flags
    assert cmd.index("--continue") > cmd.index("--dangerously-skip-permissions")


def test_build_docker_cmd_mounts_cwd():
    cr = _reload_runner("docker")
    cmd = cr._build_docker_cmd("/tmp/my-project")
    assert cmd[0] == "docker"
    assert "run" in cmd
    assert "--rm" in cmd
    assert "-i" in cmd
    # Must mount the project dir as /work
    mount_arg = cmd[cmd.index("-v") + 1]
    assert mount_arg.endswith(":/work:rw")
    assert "/tmp/my-project" in mount_arg
    assert "-w" in cmd and cmd[cmd.index("-w") + 1] == "/work"


def test_build_docker_cmd_forwards_api_key_env_only():
    cr = _reload_runner("docker")
    cmd = cr._build_docker_cmd("/tmp/x")
    # ``-e ANTHROPIC_API_KEY`` (no =VALUE) passes the host env var
    # value into the container — the key is never baked into argv or
    # the image. ps will not see it.
    assert "-e" in cmd
    e_idx = cmd.index("-e")
    assert cmd[e_idx + 1] == "ANTHROPIC_API_KEY"


def test_build_docker_cmd_uses_image_tag():
    cr = _reload_runner("docker")
    cmd = cr._build_docker_cmd("/tmp/x")
    assert "jarvis-claude:latest" in cmd


def test_build_docker_cmd_resource_caps_present():
    cr = _reload_runner("docker")
    cmd = cr._build_docker_cmd("/tmp/x")
    # Cap memory and CPU so a runaway container can't exhaust the host.
    assert "--memory=2g" in cmd
    assert "--cpus=1" in cmd


def test_build_docker_cmd_extra_flags_after_image():
    cr = _reload_runner("docker")
    cmd = cr._build_docker_cmd("/tmp/x", extra_flags=["--continue"])
    # Extra flags must come AFTER the image name so they're passed to
    # the entrypoint (claude), not to docker run.
    image_idx = cmd.index("jarvis-claude:latest")
    assert cmd.index("--continue") > image_idx


def test_docker_cmd_resolves_relative_cwd():
    cr = _reload_runner("docker")
    cmd = cr._build_docker_cmd("./relative-path")
    mount_arg = cmd[cmd.index("-v") + 1]
    # Should be absolute after resolve()
    assert mount_arg.startswith("/")
