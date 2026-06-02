"""
Working-directory allowlist for the sidecar ``/spawn`` endpoint.

Threat:
    ``/spawn`` runs ``claude -p --dangerously-skip-permissions`` on the
    macOS host with a ``cwd`` chosen by JARVIS, which sources the value
    from LLM-classified intent. With permission checks bypassed, the
    chosen ``cwd`` is the only structural guard on what claude does on
    disk. See SECURITY.md "Sidecar /spawn" subsection.

Design:
    Single chokepoint, ``assert_allowed_workdir(path, label)``:
      1. Original input must NOT itself be a symlink (subtree symlinks
         under the workdir ARE accepted — same as today's claude_runner).
      2. After ``Path.expanduser().resolve(strict=False)``, the resolved
         path must:
            (a) NOT match any entry in the HARD-DENY list (home itself,
                ~/Library, ~/.ssh, ~/.aws, ~/.config, ~/.gnupg,
                ~/.kube, ~/.docker, anything with a ``.env`` or ``.git``
                component);
            (b) be a subpath of at least one allowlist root;
            (c) exist as a directory.

    Allowlist roots:
        ~/Desktop                  — default, where projects are created
        JARVIS_EXTRA_PROJECT_DIRS  — comma-separated env var, opt-in

    Notably absent from the default: ``~/Development`` and the JARVIS
    repo root. Operators who want to allow other roots set the env var.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("jarvis_sidecar.cwd_allowlist")

# Path component names that are NEVER admitted, even if a root would
# otherwise cover them. Compared against each part of the resolved path.
_DENIED_NAMES: frozenset[str] = frozenset({
    ".ssh", ".aws", ".config", ".gnupg", ".kube", ".docker",
    ".git",
})

# Workdir must not EQUAL home itself. (Subpaths like ~/Desktop are legit.)
def _denied_exact() -> list[Path]:
    return [_resolved(Path.home())]


# Workdir must not equal OR be under any of these, regardless of
# allowlist roots. Even if a misconfigured JARVIS_EXTRA_PROJECT_DIRS
# would admit them, deny.
def _denied_prefixes() -> list[Path]:
    home = Path.home()
    return [_resolved(home / d) for d in (
        "Library", ".ssh", ".aws", ".config", ".gnupg", ".kube", ".docker",
    )]


def _resolved(p: Path) -> Path:
    return p.expanduser().resolve(strict=False)


def _read_allowlist_roots() -> list[Path]:
    """Build the allowlist at call time so test env vars take effect."""
    roots: list[Path] = [_resolved(Path.home() / "Desktop")]
    extra = os.getenv("JARVIS_EXTRA_PROJECT_DIRS", "")
    for raw in extra.split(","):
        s = raw.strip()
        if s:
            roots.append(_resolved(Path(s)))
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


def _has_denied_component(p: Path) -> bool:
    return any(part in _DENIED_NAMES for part in p.parts)


def _has_dotenv_component(p: Path) -> bool:
    # ``.env``, ``.env.local``, ``.envrc``, etc.
    return any(part == ".env" or part.startswith(".env.") or part == ".envrc"
               for part in p.parts)


def check_workdir(path: str | Path | None) -> tuple[bool, str]:
    """Return (ok, reason). ``reason`` empty on success.

    Performs in order: not-empty, not-a-symlink-as-input, resolves, no
    denied components, not under a denied prefix, under at least one
    allowlist root, exists as a directory.
    """
    if not path:
        return False, "workdir is empty"

    original = Path(path).expanduser()
    try:
        if original.is_symlink():
            return False, "workdir input itself is a symlink"
    except OSError as e:
        return False, f"workdir stat failed: {e}"

    try:
        candidate = _resolved(Path(path))
    except OSError as e:
        return False, f"workdir resolve failed: {e}"

    if _has_denied_component(candidate):
        return False, "workdir contains a denied component (.ssh/.aws/.config/.git/etc)"
    if _has_dotenv_component(candidate):
        return False, "workdir contains a .env-style component"

    # Unconditional denies — apply even if a misconfigured allowlist root
    # would otherwise admit the path.
    for denied in _denied_exact():
        if candidate == denied:
            return False, f"workdir equals denied path {denied}"
    for prefix in _denied_prefixes():
        if candidate == prefix or _is_subpath(candidate, prefix):
            return False, f"workdir is inside denied prefix {prefix}"

    if not _inside_any_root(candidate):
        roots = ", ".join(str(r) for r in _read_allowlist_roots())
        return False, f"workdir outside allowlist ({roots})"

    if not candidate.exists():
        return False, "workdir does not exist"
    if not candidate.is_dir():
        return False, "workdir is not a directory"

    return True, ""


def _inside_any_root(candidate: Path) -> bool:
    for root in _read_allowlist_roots():
        if candidate == root or _is_subpath(candidate, root):
            return True
    return False


def assert_allowed_workdir(path: str | Path | None, label: str = "workdir") -> None:
    """Raise ``ValueError`` if ``path`` is not an allowed workdir.

    Callers should catch ``ValueError`` at the spawn site, return HTTP
    400 with the reason, and append a `verb=reject` audit log line.
    """
    ok, reason = check_workdir(path)
    if not ok:
        raise ValueError(f"refusing {label}={path!r}: {reason}")
