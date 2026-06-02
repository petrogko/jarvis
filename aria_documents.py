"""
Aria's document store — text documents (contracts, term sheets, P&Ls,
school reports, anything text-paste-able) she can read across conversations.

Storage: encrypted SQLite table in the existing memory DB. FTS5 mirror for
keyword search (graceful degrade when SQLCipher lacks FTS5, same pattern
as conversations.messages_fts).

v1 scope:
- Text/markdown content only — paste via API
- Title-based reference: when his turn mentions a document title, the
  full document is injected into her prompt context
- Per-document size cap (50 KiB) so a single doc can't dominate the prompt

Deferred:
- PDF extraction (sidecar pdftotext / pymupdf in its own venv, GPL boundary)
- Frontend UI for upload (modal + drag/drop)
- Document chunking + FTS-based retrieval for unnamed-doc queries
- Per-doc metadata (counterparty, signed/draft, source, tags)
"""

from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# Per-document size cap. Big enough for a typical term sheet (5–15 pages
# of dense text), small enough that injecting the whole doc into the
# prompt doesn't blow the context window in normal use.
MAX_CONTENT_BYTES = 50 * 1024


def _get_conn():
    """Open the encrypted memory DB. Raises VaultLockedError if locked."""
    import vault
    sess = vault.session()
    if sess is None:
        raise vault.VaultLockedError("aria_documents called while vault is locked")
    conn = sess.memory_conn
    if not getattr(sess, "_aria_documents_schema_ready", False):
        _init_schema(conn)
        sess._aria_documents_schema_ready = True
    return conn


def _fts5_available(conn) -> bool:
    """Same probe as conversations.py — pysqlcipher3 wheels sometimes
    ship without FTS5; we degrade gracefully when missing."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_doc_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts5_doc_probe")
        return True
    except Exception:
        return False


def _has_documents_fts(conn) -> bool:
    cached = getattr(conn, "_jarvis_has_documents_fts", None)
    if cached is not None:
        return cached
    try:
        conn.execute("SELECT 1 FROM aria_documents_fts LIMIT 0")
        ok = True
    except Exception:
        ok = False
    try:
        conn._jarvis_has_documents_fts = ok  # type: ignore[attr-defined]
    except Exception:
        pass
    return ok


def _init_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS aria_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            content_bytes INTEGER NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_aria_documents_title ON aria_documents(title);
        """
    )
    conn.commit()
    if _fts5_available(conn):
        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS aria_documents_fts USING fts5(
                title,
                content,
                tokenize='porter unicode61'
            );
            """
        )
        conn.commit()


def create_document(title: str, content: str) -> int:
    """Insert a new document. Returns the new id. Title is whatever the
    caller provides (the user-facing name); content is the body. Strict
    on size — raises ValueError if the body exceeds MAX_CONTENT_BYTES."""
    title = (title or "").strip()
    if not title:
        raise ValueError("title is empty")
    if len(title) > 200:
        raise ValueError("title too long (>200 chars)")
    content = content or ""
    body_bytes = len(content.encode("utf-8"))
    if body_bytes > MAX_CONTENT_BYTES:
        raise ValueError(f"content exceeds {MAX_CONTENT_BYTES} bytes")
    conn = _get_conn()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO aria_documents (title, content, content_bytes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (title, content, body_bytes, now, now),
    )
    doc_id = cur.lastrowid
    if _has_documents_fts(conn):
        conn.execute(
            "INSERT INTO aria_documents_fts (rowid, title, content) VALUES (?, ?, ?)",
            (doc_id, title, content),
        )
    conn.commit()
    log.info("aria_documents: created #%d (%s, %d bytes)", doc_id, title, body_bytes)
    return doc_id


def list_documents() -> list[dict]:
    """Return all documents, newest first, without bodies (for listing)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, title, content_bytes, created_at, updated_at "
        "FROM aria_documents ORDER BY created_at DESC"
    ).fetchall()
    return [
        {
            "id": int(r["id"]) if hasattr(r, "keys") else int(r[0]),
            "title": r["title"] if hasattr(r, "keys") else r[1],
            "content_bytes": int(r["content_bytes"]) if hasattr(r, "keys") else int(r[2]),
            "created_at": float(r["created_at"]) if hasattr(r, "keys") else float(r[3]),
            "updated_at": float(r["updated_at"]) if hasattr(r, "keys") else float(r[4]),
        }
        for r in rows
    ]


def get_document(doc_id: int) -> Optional[dict]:
    """Return full document by id, or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, title, content, content_bytes, created_at, updated_at "
        "FROM aria_documents WHERE id = ?",
        (int(doc_id),),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]) if hasattr(row, "keys") else int(row[0]),
        "title": row["title"] if hasattr(row, "keys") else row[1],
        "content": row["content"] if hasattr(row, "keys") else row[2],
        "content_bytes": int(row["content_bytes"]) if hasattr(row, "keys") else int(row[3]),
        "created_at": float(row["created_at"]) if hasattr(row, "keys") else float(row[4]),
        "updated_at": float(row["updated_at"]) if hasattr(row, "keys") else float(row[5]),
    }


def delete_document(doc_id: int) -> bool:
    """Delete a document by id. Returns True if a row was deleted."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM aria_documents WHERE id = ?", (int(doc_id),))
    deleted = cur.rowcount > 0
    if deleted and _has_documents_fts(conn):
        conn.execute("DELETE FROM aria_documents_fts WHERE rowid = ?", (int(doc_id),))
    conn.commit()
    if deleted:
        log.info("aria_documents: deleted #%d", doc_id)
    return deleted


def find_referenced_documents(text: str, max_docs: int = 2) -> list[dict]:
    """Detect which stored document(s) the user is referencing in `text`,
    based on case-insensitive substring match against stored titles. Returns
    full document dicts (with content) for up to `max_docs` hits.

    A title matches if the user's text contains the title as a substring,
    case-insensitively. Short titles (< 4 chars) are skipped to avoid
    spurious matches on common words. Multiple matches → return all
    (capped), most-recently-updated first.
    """
    t = (text or "").strip()
    if len(t) < 4:
        return []
    try:
        conn = _get_conn()
    except Exception:
        return []
    lower_text = t.lower()
    rows = conn.execute(
        "SELECT id, title, content, content_bytes, created_at, updated_at "
        "FROM aria_documents ORDER BY updated_at DESC"
    ).fetchall()
    hits: list[dict] = []
    for r in rows:
        title = r["title"] if hasattr(r, "keys") else r[1]
        if len(title) < 4:
            continue
        if title.lower() in lower_text:
            hits.append({
                "id": int(r["id"]) if hasattr(r, "keys") else int(r[0]),
                "title": title,
                "content": r["content"] if hasattr(r, "keys") else r[2],
                "content_bytes": int(r["content_bytes"]) if hasattr(r, "keys") else int(r[3]),
                "created_at": float(r["created_at"]) if hasattr(r, "keys") else float(r[4]),
                "updated_at": float(r["updated_at"]) if hasattr(r, "keys") else float(r[5]),
            })
        if len(hits) >= max_docs:
            break
    return hits
