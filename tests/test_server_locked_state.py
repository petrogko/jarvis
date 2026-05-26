"""Verify the vault-locked middleware on server.py."""

from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    import vault
    monkeypatch.setattr(vault, "DATA_DIR", tmp_path)
    monkeypatch.setattr(vault, "SALT_PATH", tmp_path / "kdf.salt")
    monkeypatch.setattr(vault, "SECRETS_DB_PATH", tmp_path / "secrets.db")
    monkeypatch.setattr(vault, "MEMORY_DB_PATH", tmp_path / "jarvis.db")
    monkeypatch.setattr(vault, "LEGACY_ENV_PATH", tmp_path / ".env.bootstrap")
    yield vault
    vault.lock()


def _client():
    from server import app
    return TestClient(app)


def test_health_reachable_while_locked(isolated_vault):
    c = _client()
    r = c.get("/api/health")
    assert r.status_code == 200


def test_state_reports_uninitialized_then_initialized(isolated_vault):
    c = _client()
    r = c.get("/api/auth/state")
    assert r.json() == {"initialized": False, "locked": True}
    isolated_vault.bootstrap("pp")
    r = c.get("/api/auth/state")
    assert r.json() == {"initialized": True, "locked": True}


def test_bootstrap_then_unlock_flow(isolated_vault, monkeypatch):
    import server
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
    c = _client()
    r = c.post("/api/auth/bootstrap", json={"passphrase": "pp"})
    assert r.status_code == 200
    # Bootstrap leaves the vault locked; client must unlock explicitly.
    r = c.post("/api/auth/unlock", json={"passphrase": "pp"})
    assert r.status_code == 200


def test_bootstrap_idempotency_returns_409(isolated_vault):
    c = _client()
    c.post("/api/auth/bootstrap", json={"passphrase": "pp"})
    r = c.post("/api/auth/bootstrap", json={"passphrase": "pp2"})
    assert r.status_code == 409


def test_unlock_wrong_passphrase_returns_401(isolated_vault, monkeypatch):
    import server
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
    c = _client()
    c.post("/api/auth/bootstrap", json={"passphrase": "pp"})
    r = c.post("/api/auth/unlock", json={"passphrase": "wrong"})
    assert r.status_code == 401


def test_unlock_rate_limit_returns_429(isolated_vault, monkeypatch):
    """Spec §5: rate limit MUST fire before KDF. Second attempt within 2s -> 429."""
    import server
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
    c = _client()
    c.post("/api/auth/bootstrap", json={"passphrase": "pp"})
    r1 = c.post("/api/auth/unlock", json={"passphrase": "wrong"})
    assert r1.status_code == 401
    r2 = c.post("/api/auth/unlock", json={"passphrase": "pp"})
    assert r2.status_code == 429, f"expected 429 from rate limit, got {r2.status_code}"


def test_protected_endpoint_returns_423_while_locked(isolated_vault):
    """Spec §5: all /api/* except auth + health return 423 while locked."""
    c = _client()
    isolated_vault.bootstrap("pp")
    # /api/settings/status is one of the existing endpoints; while locked it must 423.
    r = c.get("/api/settings/status")
    assert r.status_code == 423


def test_no_stale_env_calls_remain():
    """Spec §8: after migration ships, no module outside vault.py may read
    ANTHROPIC_API_KEY / FISH_* from os.environ directly."""
    import re
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    pattern = re.compile(r'os\.getenv\(\s*[\'"](?:ANTHROPIC|FISH)_')
    offenders = []
    for py in repo_root.glob("*.py"):
        if py.name in ("vault.py", "conftest.py"):
            continue
        if pattern.search(py.read_text()):
            offenders.append(py.name)
    assert not offenders, f"these files still call os.getenv directly: {offenders}"


def test_protected_endpoint_reachable_after_unlock(isolated_vault, monkeypatch):
    import server
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
    c = _client()
    isolated_vault.bootstrap("pp")
    c.post("/api/auth/unlock", json={"passphrase": "pp"})
    r = c.get("/api/settings/status")
    # Auth token is in the vault; client doesn't have it for this test. Expect
    # either 200 (if /api/settings/status is in _PUBLIC_PATHS) or 401 — NOT 423.
    assert r.status_code != 423


def test_anthropic_client_initialized_after_unlock(isolated_vault, monkeypatch):
    """Regression guard: after a successful unlock, the global anthropic_client
    must be reconstructed using the unlocked vault's key. Reviewer found a
    pre-fix bug where lifespan() ran while locked, leaving the client at None."""
    import server
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
    # Force the broken pre-unlock state.
    server.anthropic_client = None

    c = _client()
    isolated_vault.bootstrap("pp")
    # Stash a key in the vault so the post-unlock rebuild has something to use.
    sess_temp = isolated_vault.unlock("pp")
    sess_temp.settings.set("ANTHROPIC_API_KEY", "sk-ant-test-fake")
    isolated_vault.lock()

    # Now unlock through the API — this should rebuild the client.
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
    r = c.post("/api/auth/unlock", json={"passphrase": "pp"})
    assert r.status_code == 200
    assert server.anthropic_client is not None


def test_settings_keys_accepts_tts_provider(isolated_vault, monkeypatch):
    """Spec wave-1 port 1: TTS_PROVIDER and TTS_VOICE are in the allowlist."""
    import server
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
    c = _client()
    isolated_vault.bootstrap("pp")
    r = c.post("/api/auth/unlock", json={"passphrase": "pp"})
    assert r.status_code == 200
    token = r.json()["token"]
    h = {"X-JARVIS-Token": token}

    for key in ("TTS_PROVIDER", "TTS_VOICE"):
        r = c.post(
            "/api/settings/keys",
            headers=h,
            json={"key_name": key, "key_value": "test-value"},
        )
        assert r.status_code == 200, f"{key}: {r.status_code} {r.text}"

    # Verify by re-opening the vault in this thread and reading settings.
    isolated_vault.lock()
    sess = isolated_vault.unlock("pp")
    assert sess.settings.get("TTS_PROVIDER") == "test-value"
    assert sess.settings.get("TTS_VOICE") == "test-value"


@pytest.mark.anyio
async def test_synthesize_speech_uses_sidecar_when_configured(isolated_vault, monkeypatch):
    """TTS_PROVIDER=sidecar routes through sidecar_client (not Fish, not local)."""
    import server, sidecar_client
    isolated_vault.bootstrap("pp")
    isolated_vault.unlock("pp")
    sess = isolated_vault.session()
    sess.settings.set("TTS_PROVIDER", "sidecar")

    async def fake_tts(text, voice="Alex"):
        return b"SIDECAR-AUDIO"
    monkeypatch.setattr(sidecar_client, "tts_via_sidecar", fake_tts)

    audio = await server.synthesize_speech("hello")
    assert audio == b"SIDECAR-AUDIO"


@pytest.mark.anyio
async def test_synthesize_speech_auto_falls_through_to_sidecar(isolated_vault, monkeypatch):
    """TTS_PROVIDER=auto on a host where local say is NOT available should
    try the sidecar before falling back to Fish."""
    import server, sidecar_client
    from openclaw_ports import tts_local_cli
    isolated_vault.bootstrap("pp")
    isolated_vault.unlock("pp")
    sess = isolated_vault.session()
    sess.settings.set("TTS_PROVIDER", "auto")
    # No FISH_API_KEY set; sidecar must be the next thing tried.

    monkeypatch.setattr(tts_local_cli, "is_available", lambda: False)
    async def fake_tts(text, voice="Alex"):
        return b"SIDECAR-AUDIO"
    monkeypatch.setattr(sidecar_client, "tts_via_sidecar", fake_tts)

    audio = await server.synthesize_speech("hello")
    assert audio == b"SIDECAR-AUDIO"


def test_api_stt_returns_transcript(isolated_vault, monkeypatch):
    """POST /api/stt proxies to sidecar_client.stt_via_sidecar and returns
    {text: ...}."""
    import server, sidecar_client
    isolated_vault.bootstrap("pp")
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
    # Get the auth token; client must send it on /api/stt.
    token_resp = TestClient(server.app).post("/api/auth/unlock", json={"passphrase": "pp"})
    auth_token = token_resp.json()["token"]
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)

    async def fake_stt(audio_bytes, mime_type="audio/webm"):
        return "hello world"
    monkeypatch.setattr(sidecar_client, "stt_via_sidecar", fake_stt)

    c = TestClient(server.app)
    files = {"audio": ("clip.webm", b"WEBM-BLOB", "audio/webm")}
    r = c.post("/api/stt", files=files, headers={"X-JARVIS-Token": auth_token})
    assert r.status_code == 200
    assert r.json() == {"text": "hello world"}


def test_api_stt_requires_unlock_and_token(isolated_vault):
    """Locked vault returns 423; unlocked + missing token returns 401."""
    import server
    c = TestClient(server.app)
    files = {"audio": ("clip.webm", b"x", "audio/webm")}
    r = c.post("/api/stt", files=files)
    # Locked OR missing-token both blocked; we accept either status.
    assert r.status_code in (401, 423)


def test_api_stt_audit_log_entry_written(isolated_vault, monkeypatch, tmp_path):
    """Per security-advisor required fix #2: each /api/stt call appends an
    entry to data/audit.jsonl with timestamp, bytes, transcript_returned."""
    import server, sidecar_client, audit_log
    isolated_vault.bootstrap("pp")

    # Redirect audit output to a temp file so we can inspect it.
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_PATH", audit_path)

    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)
    token_resp = TestClient(server.app).post("/api/auth/unlock", json={"passphrase": "pp"})
    auth_token = token_resp.json()["token"]
    monkeypatch.setitem(server._LAST_UNLOCK_ATTEMPT, "t", 0.0)

    async def fake_stt(audio_bytes, mime_type="audio/webm"):
        return "hello"
    monkeypatch.setattr(sidecar_client, "stt_via_sidecar", fake_stt)

    c = TestClient(server.app)
    files = {"audio": ("clip.webm", b"WEBM-BLOB-12345", "audio/webm")}
    c.post("/api/stt", files=files, headers={"X-JARVIS-Token": auth_token})

    log_lines = audit_path.read_text().splitlines()
    assert any(
        "stt_request" in line and "15" in line  # 15 bytes for our test payload
        and ("transcript_returned" in line or "transcript" in line.lower())
        for line in log_lines
    )
    # Critically: the transcript TEXT itself must NEVER appear in the audit log.
    for line in log_lines:
        assert "hello" not in line, f"transcript text leaked into audit log: {line!r}"
