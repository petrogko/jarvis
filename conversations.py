"""
Conversation persistence — store every user/assistant turn in the encrypted
vault so context survives container restarts and vault locks.

Schema lives in the same SQLCipher memory DB as dispatch_registry / memory.
DDL is lazy: first call after each unlock runs CREATE TABLE IF NOT EXISTS.

Design:
- ``conversations`` row per WS connection (or resumed connection within the
  RESUME_WINDOW_S window).
- ``messages`` row per turn — role ∈ {user, assistant, system}, content, ts.
- ``record_message`` is called by the WS handler immediately after each turn
  appends to the in-memory history list, so disk state mirrors memory.
- ``get_or_create_active_conversation`` resumes the most recent conversation
  if it had a message within RESUME_WINDOW_S; otherwise starts a new one.
- ``load_recent_messages`` pulls the last N messages for a conversation so
  the WS handler can seed ``history`` on resume.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger("jarvis.conversations")

# If the most recent conversation had a message within this many seconds, the
# next WS connect resumes it instead of starting a new one. 30 minutes lets
# a container restart + unlock retain the thread; anything older starts fresh.
RESUME_WINDOW_S: float = 30 * 60

# Sentinel attached to the unlocked VaultSession once the conversations
# schema has been ensured. Cleared on lock.
_SCHEMA_FLAG = "_conversations_schema_ready"


def _get_conn():
    """Return the live SQLCipher connection from the unlocked vault.

    Lazily initializes the conversations schema on first use after each unlock.
    Raises VaultLockedError when the vault is locked — callers all sit behind
    the vault-locked middleware so this should never trip in practice.
    """
    import vault
    sess = vault.session()
    if sess is None:
        raise vault.VaultLockedError("conversations called while vault is locked")
    if not getattr(sess, _SCHEMA_FLAG, False):
        _init_schema(sess.memory_conn)
        setattr(sess, _SCHEMA_FLAG, True)
    return sess.memory_conn


def _init_schema(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at REAL NOT NULL,
            last_message_at REAL NOT NULL,
            ended_at REAL,
            title TEXT DEFAULT '',
            message_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conv_ts ON messages(conversation_id, ts);
        CREATE INDEX IF NOT EXISTS idx_conv_last_message ON conversations(last_message_at DESC);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def create_conversation() -> int:
    """Start a new conversation. Returns the new id."""
    conn = _get_conn()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO conversations (started_at, last_message_at) VALUES (?, ?)",
        (now, now),
    )
    cid = cur.lastrowid
    conn.commit()
    log.info("conversations: new #%d", cid)
    return cid


def get_or_create_active_conversation() -> tuple[int, bool]:
    """Resume the most recent conversation if it had a message within
    RESUME_WINDOW_S; else create a new one.

    Returns ``(conversation_id, resumed)``.
    """
    conn = _get_conn()
    cutoff = time.time() - RESUME_WINDOW_S
    row = conn.execute(
        "SELECT id, last_message_at FROM conversations "
        "WHERE ended_at IS NULL AND last_message_at >= ? "
        "ORDER BY last_message_at DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    if row is not None:
        return int(row["id"]), True
    return create_conversation(), False


def end_conversation(conversation_id: int) -> None:
    """Mark a conversation as ended. Idempotent."""
    conn = _get_conn()
    conn.execute(
        "UPDATE conversations SET ended_at = COALESCE(ended_at, ?) WHERE id = ?",
        (time.time(), conversation_id),
    )
    conn.commit()


def set_title(conversation_id: int, title: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE conversations SET title = ? WHERE id = ?",
        (title[:200], conversation_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def record_message(conversation_id: int, role: str, content: str) -> int:
    """Append a turn. Role ∈ {user, assistant, system}. Returns message id."""
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"invalid role: {role!r}")
    if not content:
        return 0
    conn = _get_conn()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO messages (conversation_id, role, content, ts) "
        "VALUES (?, ?, ?, ?)",
        (conversation_id, role, content, now),
    )
    msg_id = cur.lastrowid
    conn.execute(
        "UPDATE conversations SET last_message_at = ?, "
        "message_count = message_count + 1 WHERE id = ?",
        (now, conversation_id),
    )
    conn.commit()
    return msg_id


def load_recent_messages(conversation_id: int, limit: int = 40) -> list[dict]:
    """Load the most recent N messages for a conversation, oldest-first.

    Used by the WS handler to seed ``history`` on resume so the LLM has
    immediate context.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT role, content, ts FROM messages "
        "WHERE conversation_id = ? ORDER BY ts DESC LIMIT ?",
        (conversation_id, limit),
    ).fetchall()
    # Reverse so the list is oldest-first (what the LLM expects).
    return [dict(r) for r in reversed(rows)]


def list_recent_conversations(limit: int = 20) -> list[dict]:
    """For a future History panel — list recent conversations newest-first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, started_at, last_message_at, ended_at, title, message_count "
        "FROM conversations ORDER BY last_message_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conversation_id: int) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, started_at, last_message_at, ended_at, title, message_count "
        "FROM conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    return dict(row) if row else None


def get_messages(conversation_id: int) -> list[dict]:
    """Full transcript for a conversation, oldest-first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT role, content, ts FROM messages "
        "WHERE conversation_id = ? ORDER BY ts ASC",
        (conversation_id,),
    ).fetchall()
    return [dict(r) for r in rows]
