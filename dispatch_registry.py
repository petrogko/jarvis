"""
JARVIS Dispatch Registry — tracks all active and recent project builds/dispatches.

Persists to the vault's encrypted memory DB so JARVIS always knows what he's
working on, what just finished, and what the user is likely referring to.
"""

import logging
import time

log = logging.getLogger("jarvis.dispatch")

# Set on the unlocked VaultSession when the dispatches table has been ensured.
# Cleared on lock — a fresh session re-runs the IF NOT EXISTS DDL.
_SCHEMA_FLAG = "_dispatch_schema_ready"


def _get_conn():
    """Return the live SQLCipher connection from the unlocked vault.

    Lazily initializes the dispatches schema on first use after each unlock.
    Raises VaultLockedError when the vault is locked — callers all sit behind
    the vault-locked middleware so this should never trip in practice.
    """
    import vault
    sess = vault.session()
    if sess is None:
        raise vault.VaultLockedError("dispatch_registry called while vault is locked")
    if not getattr(sess, _SCHEMA_FLAG, False):
        _init_dispatch_schema(sess.memory_conn)
        setattr(sess, _SCHEMA_FLAG, True)
    # row_factory is set centrally on the vault connection (pysqlcipher3.Row).
    return sess.memory_conn


def _init_dispatch_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            project_path TEXT NOT NULL,
            original_prompt TEXT NOT NULL,
            refined_prompt TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            claude_response TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_dispatch_status ON dispatches(status);
        CREATE INDEX IF NOT EXISTS idx_dispatch_updated ON dispatches(updated_at DESC);
    """)
    conn.commit()


def init_dispatch_db():
    """Back-compat shim. Schema init is now lazy on first _get_conn() call."""
    _get_conn()


class DispatchRegistry:
    def __init__(self):
        # Schema init is deferred to first use — vault is locked at server boot.
        # on_change(dispatch_id:int) fires after every register/update_status so
        # the server can push a live WS event without instrumenting each call
        # site. Set by server.py; stays None in tests / standalone use.
        self.on_change = None

    def _fire(self, dispatch_id: int):
        if self.on_change is None:
            return
        try:
            self.on_change(dispatch_id)
        except Exception:
            log.exception("dispatch on_change hook failed")

    def register(self, project_name: str, project_path: str, prompt: str) -> int:
        """Register a new dispatch. Returns dispatch ID."""
        conn = _get_conn()
        now = time.time()
        cur = conn.execute(
            "INSERT INTO dispatches (project_name, project_path, original_prompt, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (project_name, project_path, prompt, now, now)
        )
        dispatch_id = cur.lastrowid
        conn.commit()
        log.info(f"Registered dispatch #{dispatch_id}: {project_name}")
        self._fire(dispatch_id)
        return dispatch_id

    def update_status(self, dispatch_id: int, status: str,
                      response: str = None, summary: str = None):
        """Update dispatch status and optionally store response/summary."""
        conn = _get_conn()
        now = time.time()
        completed_at = now if status in ("completed", "failed", "timeout") else None
        # Build the SET clause from whichever optional fields were supplied, so
        # a summary-only update (the common "Running at <url>" case) persists
        # the summary instead of being silently dropped.
        sets = ["status=?", "updated_at=?", "completed_at=?"]
        params: list = [status, now, completed_at]
        if response is not None:
            sets.append("claude_response=?")
            params.append(response[:5000])
        if summary is not None:
            sets.append("summary=?")
            params.append(summary)
        params.append(dispatch_id)
        conn.execute(f"UPDATE dispatches SET {', '.join(sets)} WHERE id=?", params)
        conn.commit()
        self._fire(dispatch_id)

    def get_by_id(self, dispatch_id: int) -> dict | None:
        """Fetch a single dispatch record by id."""
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_most_recent(self) -> dict | None:
        """Get the most recently updated dispatch."""
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM dispatches ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_active(self) -> list[dict]:
        """Get all pending/building dispatches."""
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM dispatches WHERE status IN ('pending','building','planning') "
            "ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_by_name(self, name: str) -> dict | None:
        """Fuzzy match dispatch by project name."""
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM dispatches WHERE project_name LIKE ? ORDER BY updated_at DESC LIMIT 1",
            (f"%{name}%",)
        ).fetchone()
        return dict(row) if row else None

    def get_recent_for_project(self, project_name: str, max_age_seconds: int = 300) -> dict | None:
        """Return the most recent completed dispatch for a project if within max_age."""
        conn = _get_conn()
        cutoff = time.time() - max_age_seconds
        row = conn.execute(
            "SELECT * FROM dispatches WHERE project_name LIKE ? AND status = 'completed' "
            "AND completed_at IS NOT NULL AND completed_at >= ? "
            "ORDER BY completed_at DESC LIMIT 1",
            (f"%{project_name}%", cutoff)
        ).fetchone()
        return dict(row) if row else None

    def get_recent(self, limit: int = 5) -> list[dict]:
        """Get last N dispatches."""
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM dispatches ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def format_for_prompt(self) -> str:
        """Format active + recent dispatches as context for the LLM.

        Safe to call when vault is locked — returns a neutral string instead
        of crashing. server.py calls this from voice-loop construction which
        may run while still locked.
        """
        import vault
        if vault.session() is None:
            return "No active or recent dispatches."
        active = self.get_active()
        recent = self.get_recent(3)

        parts = []

        if active:
            lines = []
            for d in active:
                elapsed = int(time.time() - d["created_at"])
                lines.append(f"  - [{d['status']}] {d['project_name']} ({elapsed}s ago): {d['original_prompt'][:80]}")
            parts.append("CURRENTLY WORKING ON:\n" + "\n".join(lines))

        completed = [d for d in recent if d["status"] == "completed" and d not in active]
        if completed:
            lines = []
            for d in completed[:2]:
                lines.append(f"  - {d['project_name']}: {d['summary'][:80]}" if d["summary"] else f"  - {d['project_name']}: completed")
            parts.append("RECENTLY COMPLETED:\n" + "\n".join(lines))

        return "\n".join(parts) if parts else "No active or recent dispatches."
