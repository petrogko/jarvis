"""
Hermetic tests for /spawn. Mocks asyncio.create_subprocess_exec so we
don't need a real claude binary, and mocks os.killpg so we don't actually
send signals.

The tests focus on the security-critical paths the security-advisor
flagged: validation 400s, concurrency + per-minute caps, soft/hard
output truncation, group-kill on timeout / DELETE / output_overrun,
prompt bytes never reaching logs.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = "test-token-spawn-1234"
HEADERS = {"X-SIDECAR-Token": TOKEN}


# ---------- fixtures --------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    """Synthetic $HOME with a usable ~/Desktop allowlist root + sidecar state dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("JARVIS_EXTRA_PROJECT_DIRS", raising=False)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "token").write_text(TOKEN, encoding="utf-8")
    (tmp_path / "Desktop").mkdir()
    (tmp_path / "Library" / "Logs").mkdir(parents=True)
    # Force a fresh import so the app picks up the new env.
    for mod in list(sys.modules):
        if mod.startswith("jarvis_sidecar"):
            del sys.modules[mod]
    yield tmp_path


@pytest.fixture
def client(home):
    from jarvis_sidecar.app import create_app
    return TestClient(create_app()), home


@pytest.fixture
def project_dir(home):
    p = home / "Desktop" / "myapp"
    p.mkdir()
    return str(p)


# ---------- fake subprocess primitives --------------------------------------

class _FakeStdin:
    def __init__(self):
        self.received = bytearray()
        self.closed = False
    def write(self, data): self.received.extend(data)
    async def drain(self): return None
    def close(self): self.closed = True


class _FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks) + [b""]  # terminating EOF
    async def read(self, n):
        return self._chunks.pop(0) if self._chunks else b""


class _NeverEndsStdout:
    """Hangs forever — used for timeout tests."""
    async def read(self, n):
        await asyncio.sleep(3600)
        return b""


class _FakeProc:
    def __init__(self, *, chunks=None, returncode=0, stdout=None):
        self.stdin = _FakeStdin()
        self.stdout = stdout if stdout is not None else _FakeStdout(chunks or [])
        self._returncode = returncode
        self._exited = False
        self.pid = 12345
        self._killed_signals: list[int] = []

    @property
    def returncode(self):
        return self._returncode if self._exited else None

    async def wait(self):
        # Mark exited so returncode flips on. The watcher reads stdout to EOF
        # first then awaits wait(); by then we're done.
        self._exited = True
        return self._returncode

    def kill(self):
        self._exited = True


def _install_fake_subprocess(monkeypatch, proc_factory):
    async def fake_exec(*argv, **kwargs):
        # Capture argv + cwd for assertions.
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs.get("cwd")
        captured["start_new_session"] = kwargs.get("start_new_session")
        return proc_factory()
    captured: dict = {}
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    # killpg → just flip the proc to exited so wait() returns.
    captured["killpg_calls"] = []
    def fake_killpg(pgid, sig):
        captured["killpg_calls"].append((pgid, sig))
        # We can't reach the proc instance directly from here; the watcher's
        # _kill_group sleeps 0.5s between SIGTERM and SIGKILL. We rely on the
        # outer test to set proc._exited = True after dispatch.
    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    return captured


async def _drive_manager_to_completion(manager, prompt, workdir, timeout_s=60.0, caller_fp="testfp00"):
    """Spawn via the SpawnManager directly and await the watcher task. Returns
    the SpawnSession with terminal status set. Bypasses TestClient because it
    cancels background tasks at request close, which races the watcher."""
    session = await manager.spawn(prompt, workdir, timeout_s, caller_fp)
    if session._watcher is not None:
        await session._watcher
    return session


# ---------- happy path / argv shape ----------------------------------------

async def test_spawn_happy_path_argv_and_cwd(home, monkeypatch, project_dir):
    """Direct SpawnManager — TestClient cancels background tasks too early."""
    captured = _install_fake_subprocess(
        monkeypatch, lambda: _FakeProc(chunks=[b"hello "], returncode=0),
    )
    from jarvis_sidecar.spawn import SpawnManager
    mgr = SpawnManager()
    session = await _drive_manager_to_completion(mgr, "say hi", project_dir)

    # Argv is the exact allowlisted form, no extras.
    assert captured["argv"] == [
        "claude", "-p", "--output-format", "text", "--dangerously-skip-permissions",
    ]
    assert captured["cwd"] == project_dir
    # Required fix #7 — start_new_session=True for process group isolation.
    assert captured["start_new_session"] is True

    resp = session.to_response()
    assert resp["status"] == "finished"
    assert resp["exit_code"] == 0
    assert "hello" in resp["output"]
    assert resp["output_truncated"] is False


# ---------- validation 400s -------------------------------------------------

def test_spawn_empty_prompt_400(client, project_dir):
    r = client[0].post(
        "/spawn",
        json={"prompt": "", "workdir": project_dir},
        headers=HEADERS,
    )
    assert r.status_code == 400


def test_spawn_unknown_agent_400(client, project_dir):
    r = client[0].post(
        "/spawn",
        json={"prompt": "x", "workdir": project_dir, "agent": "codex"},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert "agent" in r.text.lower()


def test_spawn_prompt_too_large_400(client, project_dir):
    big = "x" * (64 * 1024 + 1)
    r = client[0].post(
        "/spawn",
        json={"prompt": big, "workdir": project_dir},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert "too large" in r.text


def test_spawn_timeout_out_of_range_400(client, project_dir):
    for t in (1.0, 9999.0):
        r = client[0].post(
            "/spawn",
            json={"prompt": "x", "workdir": project_dir, "timeout_s": t},
            headers=HEADERS,
        )
        assert r.status_code == 400, r.text


def test_spawn_workdir_outside_allowlist_400(client, home):
    r = client[0].post(
        "/spawn",
        json={"prompt": "x", "workdir": "/etc"},
        headers=HEADERS,
    )
    assert r.status_code == 400


def test_spawn_dotenv_workdir_400(client, home):
    bad = home / "Desktop" / "app" / ".env"
    bad.mkdir(parents=True)
    r = client[0].post(
        "/spawn",
        json={"prompt": "x", "workdir": str(bad)},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert ".env" in r.text


# ---------- auth + 404 ------------------------------------------------------

def test_spawn_requires_token(client, project_dir):
    r = client[0].post("/spawn", json={"prompt": "x", "workdir": project_dir})
    assert r.status_code == 401


def test_spawn_get_unknown_404(client):
    r = client[0].get("/spawn/does-not-exist", headers=HEADERS)
    assert r.status_code == 404


def test_spawn_delete_unknown_404(client):
    r = client[0].delete("/spawn/does-not-exist", headers=HEADERS)
    assert r.status_code == 404


# ---------- concurrency + rate caps ----------------------------------------

async def test_spawn_concurrency_cap_429(client, monkeypatch, project_dir):
    """4th simultaneous spawn returns 429."""
    _install_fake_subprocess(monkeypatch, lambda: _FakeProc(stdout=_NeverEndsStdout()))
    for _ in range(3):
        r = client[0].post(
            "/spawn",
            json={"prompt": "x", "workdir": project_dir, "timeout_s": 1800.0},
            headers=HEADERS,
        )
        assert r.status_code == 200, r.text
    r = client[0].post(
        "/spawn",
        json={"prompt": "x", "workdir": project_dir, "timeout_s": 1800.0},
        headers=HEADERS,
    )
    assert r.status_code == 429
    assert "concurrent" in r.text


async def test_spawn_rate_cap_429(home, monkeypatch, project_dir):
    """11th spawn within 60s rolling window is rejected even when nothing's running."""
    _install_fake_subprocess(monkeypatch, lambda: _FakeProc(chunks=[b"ok"], returncode=0))
    from jarvis_sidecar.spawn import SpawnManager, SpawnError
    mgr = SpawnManager()
    accepted = 0
    for _ in range(10):
        await _drive_manager_to_completion(mgr, "x", project_dir)
        accepted += 1
    with pytest.raises(SpawnError, match="rate cap"):
        await mgr.spawn("x", project_dir, 60.0, "testfp00")
    assert accepted == 10


# ---------- /health.spawn_ready --------------------------------------------

def test_health_includes_spawn_ready(client):
    r = client[0].get("/health", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert "spawn_ready" in body
    # On CI without claude installed, spawn_ready will be False — that's fine.


def test_health_still_requires_token(client):
    r = client[0].get("/health")
    assert r.status_code == 401


# ---------- audit log -------------------------------------------------------

async def test_audit_log_records_spawn_with_fingerprint_no_prompt(home, monkeypatch, project_dir):
    """Audit log line includes session_id + caller_fingerprint + prompt_bytes
    (size only) — and the prompt content NEVER appears anywhere in the file."""
    canary = "SECRET-CANARY-STRING-do-not-leak-12345"
    _install_fake_subprocess(monkeypatch, lambda: _FakeProc(chunks=[b"reply"], returncode=0))
    from jarvis_sidecar.spawn import SpawnManager
    mgr = SpawnManager()
    session = await _drive_manager_to_completion(
        mgr, f"talk about {canary}", project_dir,
    )

    log_path = home / "Library" / "Logs" / "jarvis-sidecar.log"
    raw = log_path.read_text()
    assert canary not in raw, "prompt content leaked to audit log"

    lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
    sid = session.session_id
    spawn_lines = [ln for ln in lines if ln.get("session_id") == sid]
    assert any(ln["verb"] == "spawn" for ln in spawn_lines)
    finish_lines = [ln for ln in spawn_lines if ln["verb"] in ("finished", "failed", "timeout", "killed")]
    assert finish_lines, "expected a terminal-status line"
    ln = finish_lines[0]
    assert "caller_fingerprint" in ln
    assert len(ln["caller_fingerprint"]) == 8
    assert ln["prompt_bytes"] == len(f"talk about {canary}".encode("utf-8"))
    assert "workdir" in ln
    assert "duration_ms" in ln


async def test_audit_log_records_rejection(client, home):
    """A 400 from the workdir allowlist appends a verb=reject line."""
    r = client[0].post(
        "/spawn",
        json={"prompt": "x", "workdir": "/etc"},
        headers=HEADERS,
    )
    assert r.status_code == 400

    log_path = home / "Library" / "Logs" / "jarvis-sidecar.log"
    raw = log_path.read_text()
    lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
    rejects = [ln for ln in lines if ln.get("verb") == "reject"]
    assert rejects, "expected a reject audit line"
    assert rejects[0]["workdir"] == "/etc"
    assert "reason" in rejects[0]


# ---------- DELETE / kill ---------------------------------------------------

async def test_delete_kills_running_session(client, monkeypatch, project_dir):
    captured = _install_fake_subprocess(
        monkeypatch, lambda: _FakeProc(stdout=_NeverEndsStdout()),
    )

    r = client[0].post(
        "/spawn",
        json={"prompt": "x", "workdir": project_dir, "timeout_s": 1800.0},
        headers=HEADERS,
    )
    sid = r.json()["session_id"]

    # DELETE should return immediately with status killed.
    r = client[0].delete(f"/spawn/{sid}", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "killed"
    assert r.json()["kill_reason"] == "caller"
    # killpg was called on the group.
    assert captured["killpg_calls"], "expected killpg to be invoked"


# ---------- source-scan / no-shell invariant -------------------------------

def test_spawn_source_has_no_shell_true():
    src = (ROOT / "jarvis_sidecar" / "spawn.py").read_text(encoding="utf-8")
    assert "shell=True" not in src
    assert "os.system" not in src
    # Sanity: argv list is constructed without prompt interpolation.
    assert "f\"{prompt" not in src


# ---------- output caps + timeout group-kill --------------------------------

async def test_output_soft_truncation(home, monkeypatch, project_dir):
    """Output beyond OUTPUT_MAX_BYTES (1 MiB) is dropped; output_truncated=True."""
    # Emit slightly over 1 MiB across two chunks, then EOF.
    big = b"A" * (1024 * 1024 + 4096)
    _install_fake_subprocess(monkeypatch, lambda: _FakeProc(chunks=[big], returncode=0))
    from jarvis_sidecar.spawn import SpawnManager
    from jarvis_sidecar import config
    mgr = SpawnManager()
    session = await _drive_manager_to_completion(mgr, "x", project_dir)
    assert session.output_truncated is True
    assert len(session.output) == config.OUTPUT_MAX_BYTES
    assert session.status == "finished"  # natural exit; cap is soft


async def test_output_hard_cap_kills_group(home, monkeypatch, project_dir):
    """Output beyond OUTPUT_HARD_CAP_BYTES (4 MiB) kills the process group;
    status='killed', kill_reason='output_overrun'."""
    huge = b"B" * (5 * 1024 * 1024)  # 5 MiB > 4 MiB hard cap
    captured = _install_fake_subprocess(
        monkeypatch, lambda: _FakeProc(chunks=[huge], returncode=0),
    )
    from jarvis_sidecar.spawn import SpawnManager
    mgr = SpawnManager()
    session = await _drive_manager_to_completion(mgr, "x", project_dir)
    assert session.status == "killed"
    assert session.kill_reason == "output_overrun"
    assert session.output_truncated is True
    assert captured["killpg_calls"], "expected killpg on hard-cap overrun"


async def test_timeout_kills_group(home, monkeypatch, project_dir):
    """A child that never exits is killed at timeout_s; status='timeout'."""
    captured = _install_fake_subprocess(
        monkeypatch, lambda: _FakeProc(stdout=_NeverEndsStdout()),
    )
    from jarvis_sidecar.spawn import SpawnManager
    mgr = SpawnManager()
    # Use a small timeout via the manager directly (bypasses the API's min cap).
    session = await mgr.spawn("x", project_dir, 0.3, "testfp00")
    await session._watcher
    assert session.status == "timeout"
    assert session.kill_reason == "timeout"
    assert captured["killpg_calls"], "expected killpg on timeout"
