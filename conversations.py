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


def _fts5_available(conn) -> bool:
    """Some SQLCipher builds (notably the pysqlcipher3 wheel used in CI)
    omit FTS5. Detect at runtime so the rest of the schema still works."""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)"
        )
        conn.execute("DROP TABLE temp._fts5_probe")
        return True
    except Exception:
        return False


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

    # FTS index over messages.content for cross-conversation recall.
    # Created only when the SQLCipher build supports FTS5 — production
    # container does; CI's pysqlcipher3 wheel sometimes doesn't. When
    # unavailable, search_messages_fts returns [] silently and the rest
    # of the conversations API works normally.
    if _fts5_available(conn):
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                conversation_id UNINDEXED,
                role UNINDEXED,
                ts UNINDEXED,
                tokenize='porter unicode61'
            );
        """)
        conn.commit()
        # Backfill the FTS table on first init after unlock if it's empty
        # but `messages` already has rows. Idempotent across restarts.
        fts_count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if fts_count == 0 and msg_count > 0:
            log.info("conversations: backfilling messages_fts (%d rows)", msg_count)
            conn.execute(
                "INSERT INTO messages_fts (rowid, content, conversation_id, role, ts) "
                "SELECT id, content, conversation_id, role, ts FROM messages"
            )
            conn.commit()
    else:
        log.warning("conversations: FTS5 unavailable in this SQLCipher build — semantic recall disabled")


def _has_messages_fts(conn) -> bool:
    """Cheap runtime check (used in hot path). Cached on the connection."""
    cached = getattr(conn, "_jarvis_has_messages_fts", None)
    if cached is not None:
        return cached
    try:
        conn.execute("SELECT 1 FROM messages_fts LIMIT 0")
        ok = True
    except Exception:
        ok = False
    try:
        conn._jarvis_has_messages_fts = ok  # type: ignore[attr-defined]
    except Exception:
        pass
    return ok


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
    # Mirror into the FTS index when available. Skip silently if this
    # SQLCipher build lacks FTS5 (e.g. CI's pysqlcipher3 wheel).
    if _has_messages_fts(conn):
        conn.execute(
            "INSERT INTO messages_fts (rowid, content, conversation_id, role, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (msg_id, content, conversation_id, role, now),
        )
    conn.execute(
        "UPDATE conversations SET last_message_at = ?, "
        "message_count = message_count + 1 WHERE id = ?",
        (now, conversation_id),
    )
    conn.commit()
    return msg_id


def search_messages_fts(
    query: str,
    k: int = 5,
    exclude_conversation_id: Optional[int] = None,
    max_age_days: Optional[int] = None,
) -> list[dict]:
    """Full-text search across all stored messages. Returns up to k results
    ranked by FTS5 BM25 (lower is more relevant). Filters:

    - exclude_conversation_id: drop matches from the live conversation
      (Aria shouldn't surface what she just said in this turn as "history")
    - max_age_days: drop matches older than N days (default: no cap)

    The query string is matched as-is by FTS5. Multi-word queries become
    implicit AND. We strip FTS5 syntax characters that would break the
    parse (quotes, AND/OR/NEAR operators in caps) so user-supplied text
    can flow in safely without an explicit escape pass at every callsite.
    """
    q = (query or "").strip()
    if not q:
        return []
    # Strip FTS5 syntax that could cause parse errors with raw user text.
    # We're matching content, not constructing queries — keep it simple.
    import re as _re
    q = _re.sub(r'["()]', " ", q)
    q = _re.sub(r"\b(AND|OR|NOT|NEAR)\b", " ", q)
    q = _re.sub(r"\s+", " ", q).strip()
    # Require at least one alphanumeric token after sanitization.
    if not _re.search(r"[A-Za-z0-9]", q):
        return []
    # Tokens get OR'd so we don't drop on every-word-must-match — short
    # voice utterances rarely overlap on every word with the stored
    # message; ranking handles relevance.
    tokens = [t for t in q.split() if t]
    if not tokens:
        return []
    fts_query = " OR ".join(tokens)

    conn = _get_conn()
    if not _has_messages_fts(conn):
        return []
    where_extra = ""
    params: list = [fts_query]
    if exclude_conversation_id is not None:
        where_extra += " AND m.conversation_id != ?"
        params.append(int(exclude_conversation_id))
    if max_age_days is not None and max_age_days > 0:
        cutoff = time.time() - (max_age_days * 86400)
        where_extra += " AND m.ts >= ?"
        params.append(cutoff)
    params.append(int(k))

    sql = f"""
        SELECT m.id, m.conversation_id, m.role, m.content, m.ts,
               bm25(messages_fts) AS score
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        WHERE messages_fts MATCH ?
        {where_extra}
        ORDER BY score
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        log.warning("search_messages_fts failed: %s", e)
        return []
    return [
        {
            "id": int(r["id"]) if hasattr(r, "keys") else int(r[0]),
            "conversation_id": int(r["conversation_id"]) if hasattr(r, "keys") else int(r[1]),
            "role": r["role"] if hasattr(r, "keys") else r[2],
            "content": r["content"] if hasattr(r, "keys") else r[3],
            "ts": float(r["ts"]) if hasattr(r, "keys") else float(r[4]),
            "score": float(r["score"]) if hasattr(r, "keys") else float(r[5]),
        }
        for r in rows
    ]


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
