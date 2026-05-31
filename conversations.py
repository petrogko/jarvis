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
            message_count INTEGER DEFAULT 0,
            expires_at REAL
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
        CREATE INDEX IF NOT EXISTS idx_conv_expires ON conversations(expires_at) WHERE expires_at IS NOT NULL;
    """)
    # Migration for existing vaults: add expires_at if missing.
    try:
        conn.execute("ALTER TABLE conversations ADD COLUMN expires_at REAL")
    except Exception:
        pass  # column already exists
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


_CONV_COLS = "id, started_at, last_message_at, ended_at, title, message_count, expires_at"


def list_recent_conversations(limit: int = 20) -> list[dict]:
    """For a future History panel — list recent conversations newest-first."""
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT {_CONV_COLS} FROM conversations ORDER BY last_message_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conversation_id: int) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute(
        f"SELECT {_CONV_COLS} FROM conversations WHERE id = ?",
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


# ---------------------------------------------------------------------------
# Phase 1B — Deletion + TTL (advisor-cleared GO-WITH-FIXES, 2026-05-30)
# ---------------------------------------------------------------------------

# REST PATCH's ttl_seconds is capped at 30 days. Per advisor required fix #3,
# the voice-path cap (also 30 days) is mirrored at the REST boundary so a
# direct API caller can't set an effectively-unbounded TTL.
TTL_MAX_S: float = 30 * 86400.0


def delete_conversation(conversation_id: int) -> int:
    """Hard delete a conversation and its messages. Returns the count of
    deleted messages. Idempotent: deleting an unknown id returns 0.

    Per the spec, this is intentionally NOT a soft delete — counsel-grade
    means the conversation is gone. The caller is responsible for writing
    the audit-log entry (audit shape lives in server.py).
    """
    conn = _get_conn()
    # Count messages first so we can return it.
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    n = int(row["n"]) if row else 0
    if n == 0 and not get_conversation(conversation_id):
        return 0  # nothing to do
    conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
    conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    conn.commit()
    log.info("conversations: deleted #%d (%d messages)", conversation_id, n)
    return n


def set_expiry(conversation_id: int, ttl_seconds: Optional[float]) -> bool:
    """Set or clear the expires_at timestamp. Returns True if the row was
    updated, False if the conversation doesn't exist.

    Per advisor required fix #3, ``ttl_seconds`` is clamped to ``[0, TTL_MAX_S]``.
    Negative / None → clear the expiry (conversation is permanent again).
    """
    conn = _get_conn()
    if ttl_seconds is None or ttl_seconds <= 0:
        expires_at = None
    else:
        ttl = min(float(ttl_seconds), TTL_MAX_S)
        expires_at = time.time() + ttl
    cur = conn.execute(
        "UPDATE conversations SET expires_at = ? WHERE id = ?",
        (expires_at, conversation_id),
    )
    conn.commit()
    return cur.rowcount > 0


def find_expired(now: Optional[float] = None,
                 active_ids: Optional[set[int]] = None) -> list[int]:
    """Return ids of conversations whose ``expires_at`` is at or before
    ``now``, EXCLUDING any id in ``active_ids``.

    The ``active_ids`` parameter is the load-bearing piece of advisor
    required fix #6: a sweeper run mid-conversation must NOT delete the
    conversation the user is actively talking through. The WS handler
    populates the set on connect / clears on disconnect.
    """
    if now is None:
        now = time.time()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id FROM conversations "
        "WHERE expires_at IS NOT NULL AND expires_at <= ? "
        "ORDER BY expires_at ASC",
        (now,),
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    if active_ids:
        ids = [cid for cid in ids if cid not in active_ids]
    return ids


def run_sweeper_once(active_ids: Optional[set[int]] = None) -> dict:
    """One pass of the expiry sweeper. Returns ``{deleted_conversations:n,
    deleted_messages:n}``. NEVER raises — per advisor required fix #4 each
    per-id failure is isolated and logged with the exception class only.
    """
    summary = {"deleted_conversations": 0, "deleted_messages": 0}
    try:
        ids = find_expired(active_ids=active_ids)
    except Exception as e:
        log.warning("conversations.sweeper: find_expired failed (%s)", type(e).__name__)
        return summary
    for cid in ids:
        try:
            msgs = delete_conversation(cid)
            summary["deleted_conversations"] += 1
            summary["deleted_messages"] += msgs
        except Exception as e:
            # Per-id isolation — one bad row doesn't stop the sweeper.
            log.warning(
                "conversations.sweeper: delete #%d failed (%s); continuing",
                cid, type(e).__name__,
            )
    return summary
