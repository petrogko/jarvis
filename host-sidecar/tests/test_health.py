"""
Hermetic tests for the /health endpoint via FastAPI TestClient.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Isolated state dir + a fixed test token."""
    monkeypatch.setenv("JARVIS_SIDECAR_STATE_DIR", str(tmp_path))
    (tmp_path / "token").write_text("test-token-12345", encoding="utf-8")
    # Re-import the app after env is set so it reads the fresh token.
    if "jarvis_sidecar.app" in sys.modules:
        del sys.modules["jarvis_sidecar.app"]
    from jarvis_sidecar.app import create_app
    return TestClient(create_app())


def test_health_requires_token(client):
    r = client.get("/health")
    assert r.status_code == 401


def test_health_rejects_wrong_token(client):
    r = client.get("/health", headers={"X-SIDECAR-Token": "wrong"})
    assert r.status_code == 401


def test_health_ok_with_correct_token(client):
    r = client.get("/health", headers={"X-SIDECAR-Token": "test-token-12345"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "whisper_model" in body
    assert "say_available" in body
    assert "piper_available" in body
