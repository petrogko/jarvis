"""
Aria's persistent profile — what she knows about HIM, across all conversations.

The gap between "assistant" and "digital partner" isn't model capability. It's
what she persistently knows: who he is, his family, his stakes, his patterns.
Without this, she's a smart consultant meeting him for the first time every
session. With it, she's the one who knows where he left off.

Storage: single-row markdown blob in the encrypted memory DB. She reads it
every turn (injected into the system prompt). She updates it via a
[PROFILE_NOTE: <one short observation>] marker the same way she emits
[REG:X] — the server strips the marker, appends a timestamped bullet to
the "Recent observations" section, and on next turn she sees it back.

v1 keeps it minimal: append-only, no LLM-side restructuring. v2 will add a
periodic consolidator pass that refactors observations into structured
sections (Family, Stakes, Patterns, etc.) once enough have accumulated.

Vault discipline: the table lives in the existing SQLCipher-encrypted DB,
keyed by the same Argon2id master key as everything else. Profile content
never touches plaintext disk and never appears in audit logs.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


_INITIAL_TEMPLATE = """\
# Profile

(She's just getting to know him. Each observation below is a stable fact
she's noticed across conversations — not a complete picture, just what's
landed so far.)

## Who he is

_(empty — she'll fill this in as she learns)_

## People who matter to him

_(empty)_

## Current stakes

_(empty)_

## Patterns and tells

_(empty)_

## Recent observations
"""


def _get_conn():
    """Open the encrypted memory DB. Raises VaultLockedError if locked."""
    import vault
    sess = vault.session()
    if sess is None:
        raise vault.VaultLockedError("aria_profile called while vault is locked")
    conn = sess.memory_conn
    if not getattr(sess, "_aria_profile_schema_ready", False):
        _init_schema(conn)
        sess._aria_profile_schema_ready = True
    return conn


def _init_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS aria_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            content TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    conn.commit()


def load_profile() -> str:
    """Return the current profile markdown. Initializes from template on first
    call after vault unlock. Returns empty-state markdown if vault is locked
    (caller decides whether to inject)."""
    try:
        conn = _get_conn()
    except Exception:
        return ""
    row = conn.execute(
        "SELECT content FROM aria_profile WHERE id = 1"
    ).fetchone()
    if row is None:
        now = time.time()
        conn.execute(
            "INSERT INTO aria_profile (id, content, updated_at) VALUES (1, ?, ?)",
            (_INITIAL_TEMPLATE, now),
        )
        conn.commit()
        return _INITIAL_TEMPLATE
    return row["content"] if hasattr(row, "keys") else row[0]


def append_observation(note: str) -> bool:
    """Append a single timestamped observation under the 'Recent observations'
    section. Returns True if applied, False if skipped (empty note, vault
    locked, etc.).

    The observation is exactly what Aria put in [PROFILE_NOTE: ...]. We don't
    parse it; v1 just preserves what she said. Consolidation into structured
    sections is a v2 background task.
    """
    note = (note or "").strip()
    if not note:
        return False
    if len(note) > 500:
        # Cap so a runaway LLM can't blow up the profile.
        note = note[:497] + "…"
    try:
        conn = _get_conn()
    except Exception:
        log.warning("aria_profile.append_observation: vault locked, skipping")
        return False
    content = load_profile()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bullet = f"- {today}: {note}"
    # Append under the section header. If somehow the section is missing
    # (manual edit, schema drift), append to the end with the header.
    section = "## Recent observations"
    if section in content:
        # Insert after the section header line, before any trailing blank line.
        idx = content.index(section) + len(section)
        # Make sure there's exactly one newline after the header, then bullet.
        head = content[:idx].rstrip("\n") + "\n"
        tail = content[idx:].lstrip("\n")
        new_content = head + tail + (("\n" if tail and not tail.endswith("\n") else "") + bullet + "\n")
        # Simpler: just append the bullet to the end of the file. Avoids the
        # section-shuffling complexity above for v1.
        new_content = content.rstrip() + "\n" + bullet + "\n"
    else:
        new_content = content.rstrip() + f"\n\n{section}\n{bullet}\n"
    now = time.time()
    conn.execute(
        "UPDATE aria_profile SET content = ?, updated_at = ? WHERE id = 1",
        (new_content, now),
    )
    conn.commit()
    log.info("aria_profile: appended observation (len=%d)", len(note))
    return True


def reset_profile() -> None:
    """Wipe the profile back to the initial template. Manual-only — not
    exposed to the LLM. For ops/debugging when the profile drifts."""
    try:
        conn = _get_conn()
    except Exception:
        return
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO aria_profile (id, content, updated_at) VALUES (1, ?, ?)",
        (_INITIAL_TEMPLATE, now),
    )
    conn.commit()
