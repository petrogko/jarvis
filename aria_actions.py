"""
Aria's external-action tracker — long-running tasks that span the boundary
between conversation and the real world (phone calls, emails to companies,
form submissions, multi-day negotiations).

Different shape from the existing `tasks` table (personal todos) and from
`ClaudeTaskManager` (Claude Code subprocess invocations). This is for the
Pine-AI-style work: a thing in flight with a counterparty.

v1 scope:
- `kind ∈ {call_draft}` — phone call she's prepared a script for
  (you make the call manually for now; v2 dispatches to Bland.ai / VAPI)
- `status ∈ {drafted, in_progress, completed, abandoned}`
- The full lifecycle: drafted → in_progress → completed (with outcome)
- Outcome capture: free-form notes + optional dollar amount saved/recovered
- No external execution YET — the executor lands in the next PR

Storage: encrypted SQLCipher in the existing memory DB. Same vault, same key.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

log = logging.getLogger(__name__)


VALID_KINDS = ("call_draft", "email_draft")
VALID_STATUSES = ("drafted", "in_progress", "completed", "abandoned")
MAX_PLAN_BYTES = 32 * 1024
MAX_OUTCOME_BYTES = 16 * 1024


def _get_conn():
    import vault
    sess = vault.session()
    if sess is None:
        raise vault.VaultLockedError("aria_actions called while vault is locked")
    conn = sess.memory_conn
    if not getattr(sess, "_aria_actions_schema_ready", False):
        _init_schema(conn)
        sess._aria_actions_schema_ready = True
    return conn


def _init_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS aria_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'drafted',
            vendor TEXT DEFAULT '',
            goal TEXT NOT NULL,
            phone TEXT DEFAULT '',
            plan TEXT DEFAULT '',
            outcome_notes TEXT DEFAULT '',
            outcome_value_cents INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_aria_actions_status_created
            ON aria_actions(status, created_at DESC);
        """
    )
    conn.commit()


def create_action(
    kind: str,
    goal: str,
    vendor: str = "",
    phone: str = "",
    plan: str = "",
) -> int:
    """Create a new action record. Returns the new id."""
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind!r}")
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("goal is empty")
    if len(goal) > 500:
        raise ValueError("goal too long (>500 chars)")
    if len(plan or "") > MAX_PLAN_BYTES:
        raise ValueError(f"plan exceeds {MAX_PLAN_BYTES} bytes")
    conn = _get_conn()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO aria_actions "
        "(kind, status, vendor, goal, phone, plan, created_at, updated_at) "
        "VALUES (?, 'drafted', ?, ?, ?, ?, ?, ?)",
        (kind, vendor or "", goal, phone or "", plan or "", now, now),
    )
    action_id = cur.lastrowid
    conn.commit()
    log.info("aria_actions: created #%d (%s: %s)", action_id, kind, goal[:60])
    return action_id


def update_status(action_id: int, status: str) -> bool:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    conn = _get_conn()
    now = time.time()
    if status == "completed":
        cur = conn.execute(
            "UPDATE aria_actions SET status = ?, updated_at = ?, completed_at = ? "
            "WHERE id = ?",
            (status, now, now, int(action_id)),
        )
    else:
        cur = conn.execute(
            "UPDATE aria_actions SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, int(action_id)),
        )
    conn.commit()
    return cur.rowcount > 0


def update_outcome(
    action_id: int,
    outcome_notes: str = "",
    outcome_value_cents: Optional[int] = None,
    mark_completed: bool = True,
) -> bool:
    """Record the outcome of an action. By default also marks it completed."""
    if len(outcome_notes or "") > MAX_OUTCOME_BYTES:
        raise ValueError(f"outcome_notes exceeds {MAX_OUTCOME_BYTES} bytes")
    conn = _get_conn()
    now = time.time()
    if mark_completed:
        conn.execute(
            "UPDATE aria_actions SET outcome_notes = ?, outcome_value_cents = ?, "
            "status = 'completed', updated_at = ?, completed_at = COALESCE(completed_at, ?) "
            "WHERE id = ?",
            (outcome_notes or "", outcome_value_cents, now, now, int(action_id)),
        )
    else:
        conn.execute(
            "UPDATE aria_actions SET outcome_notes = ?, outcome_value_cents = ?, "
            "updated_at = ? WHERE id = ?",
            (outcome_notes or "", outcome_value_cents, now, int(action_id)),
        )
    conn.commit()
    return True


def update_plan(action_id: int, plan: str) -> bool:
    if len(plan or "") > MAX_PLAN_BYTES:
        raise ValueError(f"plan exceeds {MAX_PLAN_BYTES} bytes")
    conn = _get_conn()
    conn.execute(
        "UPDATE aria_actions SET plan = ?, updated_at = ? WHERE id = ?",
        (plan or "", time.time(), int(action_id)),
    )
    conn.commit()
    return True


def get_action(action_id: int) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, kind, status, vendor, goal, phone, plan, "
        "outcome_notes, outcome_value_cents, "
        "created_at, updated_at, completed_at "
        "FROM aria_actions WHERE id = ?",
        (int(action_id),),
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def list_actions(status: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Newest-first list, optionally filtered by status."""
    conn = _get_conn()
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status filter: {status!r}")
        rows = conn.execute(
            "SELECT id, kind, status, vendor, goal, phone, plan, "
            "outcome_notes, outcome_value_cents, "
            "created_at, updated_at, completed_at "
            "FROM aria_actions WHERE status = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (status, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, kind, status, vendor, goal, phone, plan, "
            "outcome_notes, outcome_value_cents, "
            "created_at, updated_at, completed_at "
            "FROM aria_actions "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def delete_action(action_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM aria_actions WHERE id = ?", (int(action_id),))
    conn.commit()
    return cur.rowcount > 0


def open_actions_summary_for_prompt(max_actions: int = 5) -> str:
    """Return a compact string Aria can see in her system prompt — open
    actions she should be tracking. Empty string when there are none."""
    try:
        conn = _get_conn()
    except Exception:
        return ""
    rows = conn.execute(
        "SELECT id, kind, status, vendor, goal, created_at "
        "FROM aria_actions WHERE status IN ('drafted', 'in_progress') "
        "ORDER BY created_at DESC LIMIT ?",
        (int(max_actions),),
    ).fetchall()
    if not rows:
        return ""
    lines = []
    now = time.time()
    for r in rows:
        rid = int(r["id"]) if hasattr(r, "keys") else int(r[0])
        kind = r["kind"] if hasattr(r, "keys") else r[1]
        status = r["status"] if hasattr(r, "keys") else r[2]
        vendor = r["vendor"] if hasattr(r, "keys") else r[3]
        goal = r["goal"] if hasattr(r, "keys") else r[4]
        created = float(r["created_at"]) if hasattr(r, "keys") else float(r[5])
        age_days = int((now - created) / 86400)
        age = "today" if age_days == 0 else ("yesterday" if age_days == 1 else f"{age_days}d old")
        vendor_str = f" ({vendor})" if vendor else ""
        lines.append(f"- #{rid} [{status}] {kind}{vendor_str} — {goal} · {age}")
    return "\n".join(lines)


def _row_to_dict(r: Any) -> dict:
    def g(idx: int, key: str):
        return r[key] if hasattr(r, "keys") else r[idx]
    return {
        "id": int(g(0, "id")),
        "kind": g(1, "kind"),
        "status": g(2, "status"),
        "vendor": g(3, "vendor"),
        "goal": g(4, "goal"),
        "phone": g(5, "phone"),
        "plan": g(6, "plan"),
        "outcome_notes": g(7, "outcome_notes"),
        "outcome_value_cents": g(8, "outcome_value_cents"),
        "created_at": float(g(9, "created_at")),
        "updated_at": float(g(10, "updated_at")),
        "completed_at": (float(g(11, "completed_at")) if g(11, "completed_at") is not None else None),
    }


# ---------------------------------------------------------------------------
# Call-draft plan structure — what the LLM produces and the UI renders.
# ---------------------------------------------------------------------------

def parse_call_draft_args(target: str) -> dict:
    """Parse the [ACTION:CALL_DRAFT] target payload.

    Format: pipe-delimited key=value pairs.
        vendor=Comcast | phone=1-800-... | goal=Negotiate bill | notes=...
    Tolerant: missing keys default to empty. Only `goal` is required.
    """
    parts = [p.strip() for p in (target or "").split("|") if p.strip()]
    out = {"vendor": "", "phone": "", "goal": "", "notes": ""}
    for p in parts:
        if "=" not in p:
            # treat lone segment as goal
            if not out["goal"]:
                out["goal"] = p
            continue
        k, _, v = p.partition("=")
        k = k.strip().lower()
        v = v.strip()
        if k in out:
            out[k] = v
    return out


def parse_email_draft_args(target: str) -> dict:
    """Parse [ACTION:EMAIL_DRAFT] target payload.
    Format: pipe-delimited key=value pairs.
        recipient=billing@comcast.com | vendor=Comcast | goal=Dispute charge | notes=...
    Required: goal (or lone-segment fallback)."""
    parts = [p.strip() for p in (target or "").split("|") if p.strip()]
    out = {"recipient": "", "vendor": "", "goal": "", "notes": ""}
    for p in parts:
        if "=" not in p:
            if not out["goal"]:
                out["goal"] = p
            continue
        k, _, v = p.partition("=")
        k = k.strip().lower()
        v = v.strip()
        if k in out:
            out[k] = v
    return out


EMAIL_DRAFT_SYSTEM_PROMPT = """You are drafting an email the user will send to handle a piece of life-admin work — a refund dispute, a complaint, a subscription cancellation, a billing inquiry, a chargeback escalation. You are not sending the email; he will, from his own address.

The plan you produce is structured markdown with these sections, in this order:

## Goal
One sentence: what success looks like. Concrete (a refund of $X, a cancellation effective by date Y, a written response within Z days).

## Subject line
Exact, specific, hard to ignore. Names the issue, the account, and the desired outcome at a glance.
Example shape: "Refund request — confirmation #ABC123 — service failure Oct 14"

## To / CC (suggested)
Primary recipient address (or "you'll need to look this up"). Suggested CCs if escalation matters (consumer protection, BBB, state AG, the company's general counsel for serious matters).

## The email body
Written in his voice — first person, calm, factual, specific. Use these moves in order:
1. State the issue in one paragraph. Dates, dollar amounts, account/order/confirmation numbers up front.
2. State what was promised vs. what happened. Quote contract or marketing language if relevant.
3. State the remedy he wants. Specific. Time-bound.
4. State the consequence if it isn't resolved. Proportionate — chargeback for small money, regulatory complaint for larger, legal counsel for serious.
5. Sign off with a deadline (e.g. "I'll need a response by [date 7 business days out]").

The email should be ~200-350 words. Long enough to be taken seriously, short enough to actually be read. No threats. No emotional language. The tone is "this is going to get resolved one way or the other."

## Attachments to include
A short bulleted list of what to attach (screenshots of the original confirmation, billing statements, prior correspondence).

## What to do if no response
A one-paragraph escalation path: who to contact next (specific named office/role), what additional pressure to apply (chargeback window, regulatory complaint, social media), and a clear deadline for that escalation.

Tone: precise, calm, low-emotion, hard to dismiss. Do not use exclamation marks. Do not use words like "frustrated," "outraged," "ridiculous." Use specific facts. The reader (a customer service rep or supervisor) should be able to scan it and immediately understand what needs to happen."""


CALL_DRAFT_SYSTEM_PROMPT = """You are drafting a phone-call script the user will follow when they place this call themselves. You are not making the call — they are. Your job is to give them what they need to walk in calm and effective.

The plan you produce is structured markdown with these sections, in this order:

## Goal
One sentence: what success looks like for this call. Concrete and measurable where possible (a dollar amount, a confirmation number, a specific action by the company).

## Before you dial
Information he needs in hand. Account number, last bill date, dates and times of prior failures, specific representative names if known. List only what's actually likely needed.

## Opening line
Exact words. Short. Calm. Names the goal without bargaining.

## The script (turn by turn)
A handful of expected counterparty responses with his response to each. Cover at minimum:
- The first-line agent's deflection ("our system shows your account is paid up")
- The "I'll have to transfer you" move
- The "best I can do is X" first offer

## If they push back
The escalation moves: ask for a supervisor by name, mention competitor offers, mention churn risk, request retention specifically.

## What to write down during the call
The data he needs to capture: rep name, confirmation number, exact dollar change, effective date, callback number if they say they'll call back.

## When to stop
A clear "if you've gotten X, that's good enough" line so he doesn't over-negotiate and lose ground.

Tone: this is the friend who's been on these calls before, not a corporate playbook. Use short paragraphs. No bullet point lists where prose serves better. Do not write more than ~400 words total — voice playback length matters."""
