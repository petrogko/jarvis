"""
Working-directory allowlist for ``claude -p`` subprocess spawns.

Threat:
    Five spawn sites in this codebase launch ``claude -p
    --dangerously-skip-permissions`` with a ``cwd=`` derived from
    LLM-classified intent: the build dir, the research dir, the
    work-mode project, the QA verifier, the QA auto-retry. The LLM
    chooses which project to operate in. With ``--dangerously-skip-
    permissions``, claude can read/write any file the OS will let
    the user touch — so the chosen ``cwd`` effectively scopes the
    blast radius of every JARVIS action.

    The existing defenses are real but indirect:
      * ``_generate_project_name`` strips non-alnum/dash;
      * ``_assert_safe_path`` rejects shell metacharacters;
      * ``[ACTION:PROMPT_PROJECT]`` validates the project name.

    None of those guarantee the resolved path lands inside an
    expected root. If a future refactor changes how the path is
    composed, or if symlinks are introduced under Desktop, the
    invariant silently weakens.

Design:
    Single chokepoint, ``assert_allowed_cwd(path, label)``. Resolves
    the candidate path and tests whether it is a subpath of any
    allowlisted root. Allowlist:

      * ``~/Desktop``                  — where projects are created
      * The JARVIS repo root           — self-modify path
                                          (already gated by JARVIS_ENABLE_FIX_SELF)
      * ``JARVIS_EXTRA_PROJECT_DIRS``  — comma-separated env var
                                          for the operator's own
                                          additional roots

    Resolution is via ``Path.resolve(strict=False)`` so a yet-to-be-
    created project dir is allowed, but symlinks and ``..`` are
    flattened first — defeating ``~/Desktop/../../etc/passwd``.

    Rejection logs to audit_log so attempted escapes are observable.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("jarvis.cwd_allowlist")

ROOT = Path(__file__).resolve().parent


def _resolved(p: Path) -> Path:
    return p.expanduser().resolve(strict=False)


def _read_allowlist() -> list[Path]:
    """Build the allowlist at call time so test env vars take effect."""
    roots: list[Path] = [
        _resolved(Path.home() / "Desktop"),
        _resolved(ROOT),
    ]
    extra = os.getenv("JARVIS_EXTRA_PROJECT_DIRS", "")
    for raw in extra.split(","):
        s = raw.strip()
        if s:
            roots.append(_resolved(Path(s)))
    # De-dup while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def _is_subpath(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def is_allowed_cwd(path: str | Path | None) -> bool:
    """Return True iff ``path`` (after resolution) is inside any allowlist root."""
    if not path:
        return False
    try:
        candidate = _resolved(Path(path))
    except OSError:
        return False
    for root in _read_allowlist():
        if candidate == root or _is_subpath(candidate, root):
            return True
    return False


def assert_allowed_cwd(path: str | Path | None, label: str = "cwd") -> None:
    """Raise ``ValueError`` if ``path`` is outside the allowlist.

    Callers should catch ``ValueError`` at the spawn site, audit-log
    the rejection, and refuse to launch the subprocess.
    """
    if not is_allowed_cwd(path):
        roots = ", ".join(str(r) for r in _read_allowlist())
        raise ValueError(
            f"refusing to launch with {label}={path!r}: outside allowlist ({roots})"
        )
