"""Tests for conversations.py — vault-backed conversation persistence."""

from __future__ import annotations

import pathlib
import sys
import time

import pytest

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


@pytest.fixture
def unlocked(isolated_vault):
    isolated_vault.bootstrap("pp")
    isolated_vault.unlock("pp")
    yield isolated_vault


# ---------- create / record / load -----------------------------------------

def test_create_conversation_returns_id(unlocked):
    import conversations
    cid = conversations.create_conversation()
    assert cid >= 1
    conv = conversations.get_conversation(cid)
    assert conv is not None
    assert conv["message_count"] == 0


def test_record_message_increments_count_and_last_message_at(unlocked):
    import conversations
    cid = conversations.create_conversation()
    conv_before = conversations.get_conversation(cid)
    last_before = conv_before["last_message_at"]
    time.sleep(0.01)
    conversations.record_message(cid, "user", "hello sir")
    conversations.record_message(cid, "assistant", "hello back")
    conv_after = conversations.get_conversation(cid)
    assert conv_after["message_count"] == 2
    assert conv_after["last_message_at"] > last_before


def test_record_message_rejects_bad_role(unlocked):
    import conversations
    cid = conversations.create_conversation()
    with pytest.raises(ValueError, match="invalid role"):
        conversations.record_message(cid, "bogus", "x")


def test_record_message_skips_empty(unlocked):
    import conversations
    cid = conversations.create_conversation()
    conversations.record_message(cid, "user", "")
    assert conversations.get_conversation(cid)["message_count"] == 0


def test_load_recent_messages_oldest_first(unlocked):
    import conversations
    cid = conversations.create_conversation()
    conversations.record_message(cid, "user", "one")
    conversations.record_message(cid, "assistant", "two")
    conversations.record_message(cid, "user", "three")
    msgs = conversations.load_recent_messages(cid, limit=10)
    assert [m["content"] for m in msgs] == ["one", "two", "three"]


def test_load_recent_messages_respects_limit(unlocked):
    import conversations
    cid = conversations.create_conversation()
    for i in range(5):
        conversations.record_message(cid, "user", f"msg-{i}")
    msgs = conversations.load_recent_messages(cid, limit=2)
    assert len(msgs) == 2
    # Oldest of the trailing two — last two messages are msg-3 and msg-4.
    assert msgs[0]["content"] == "msg-3"
    assert msgs[1]["content"] == "msg-4"


# ---------- resume window --------------------------------------------------

def test_get_or_create_active_resumes_fresh_conversation(unlocked):
    import conversations
    cid = conversations.create_conversation()
    conversations.record_message(cid, "user", "ping")
    cid2, resumed = conversations.get_or_create_active_conversation()
    assert resumed is True
    assert cid2 == cid


def test_get_or_create_active_starts_new_when_window_expired(unlocked, monkeypatch):
    import conversations
    cid = conversations.create_conversation()
    conversations.record_message(cid, "user", "old")
    # Force last_message_at to be > RESUME_WINDOW_S in the past.
    conn = conversations._get_conn()
    conn.execute(
        "UPDATE conversations SET last_message_at = ? WHERE id = ?",
        (time.time() - conversations.RESUME_WINDOW_S - 60, cid),
    )
    conn.commit()
    cid2, resumed = conversations.get_or_create_active_conversation()
    assert resumed is False
    assert cid2 != cid


def test_get_or_create_active_starts_new_when_ended(unlocked):
    import conversations
    cid = conversations.create_conversation()
    conversations.record_message(cid, "user", "ping")
    conversations.end_conversation(cid)
    cid2, resumed = conversations.get_or_create_active_conversation()
    assert resumed is False
    assert cid2 != cid


def test_get_or_create_active_creates_new_when_empty(unlocked):
    import conversations
    cid, resumed = conversations.get_or_create_active_conversation()
    assert cid >= 1
    assert resumed is False


# ---------- listing --------------------------------------------------------

def test_list_recent_conversations_newest_first(unlocked):
    import conversations
    c1 = conversations.create_conversation()
    conversations.record_message(c1, "user", "first")
    time.sleep(0.01)
    c2 = conversations.create_conversation()
    conversations.record_message(c2, "user", "second")
    recent = conversations.list_recent_conversations(limit=5)
    assert [r["id"] for r in recent[:2]] == [c2, c1]


def test_get_messages_full_transcript(unlocked):
    import conversations
    cid = conversations.create_conversation()
    conversations.record_message(cid, "user", "U1")
    conversations.record_message(cid, "assistant", "A1")
    conversations.record_message(cid, "user", "U2")
    msgs = conversations.get_messages(cid)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert [m["content"] for m in msgs] == ["U1", "A1", "U2"]


# ---------- end_conversation idempotent ------------------------------------

def test_end_conversation_idempotent(unlocked):
    import conversations
    cid = conversations.create_conversation()
    conversations.end_conversation(cid)
    first = conversations.get_conversation(cid)["ended_at"]
    conversations.end_conversation(cid)
    second = conversations.get_conversation(cid)["ended_at"]
    assert first == second  # COALESCE preserves the first ended_at


# ---------- vault-locked safety --------------------------------------------

def test_record_message_raises_when_vault_locked(isolated_vault):
    """Schema init refuses without unlocked vault — caller catches and degrades."""
    import conversations, vault
    isolated_vault.bootstrap("pp")
    # Don't unlock.
    with pytest.raises(vault.VaultLockedError):
        conversations.create_conversation()
