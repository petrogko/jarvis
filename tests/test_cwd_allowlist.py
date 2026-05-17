"""
Tests for cwd_allowlist — the subpath-rooted allowlist that gates
every ``claude -p`` spawn site.
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cwd_allowlist as ca  # noqa: E402


def test_repo_root_is_allowed():
    assert ca.is_allowed_cwd(str(ROOT))


def test_desktop_subdir_is_allowed():
    desktop = pathlib.Path.home() / "Desktop"
    assert ca.is_allowed_cwd(str(desktop / "my-project"))


def test_desktop_root_itself_is_allowed():
    assert ca.is_allowed_cwd(str(pathlib.Path.home() / "Desktop"))


def test_outside_allowlist_is_rejected():
    assert not ca.is_allowed_cwd("/etc")
    assert not ca.is_allowed_cwd("/tmp/random")
    assert not ca.is_allowed_cwd("/private/var/log")


def test_path_traversal_is_normalized_then_rejected():
    desktop = pathlib.Path.home() / "Desktop"
    # ..-laden path that resolves outside Desktop must be rejected.
    sneaky = str(desktop / ".." / ".." / "etc")
    assert not ca.is_allowed_cwd(sneaky)


def test_empty_and_none_rejected():
    assert not ca.is_allowed_cwd(None)
    assert not ca.is_allowed_cwd("")


def test_env_var_extends_allowlist(monkeypatch=None):
    # Without env: /tmp/projects rejected.
    assert not ca.is_allowed_cwd("/tmp/projects/foo")
    # With env: allowed.
    old = os.environ.get("JARVIS_EXTRA_PROJECT_DIRS")
    try:
        os.environ["JARVIS_EXTRA_PROJECT_DIRS"] = "/tmp/projects, /var/tmp/work"
        assert ca.is_allowed_cwd("/tmp/projects/foo")
        assert ca.is_allowed_cwd("/var/tmp/work/x/y")
        assert not ca.is_allowed_cwd("/tmp/other")
    finally:
        if old is None:
            os.environ.pop("JARVIS_EXTRA_PROJECT_DIRS", None)
        else:
            os.environ["JARVIS_EXTRA_PROJECT_DIRS"] = old


def test_assert_raises_on_reject():
    try:
        ca.assert_allowed_cwd("/etc/passwd", label="research_cwd")
    except ValueError as e:
        assert "research_cwd" in str(e)
        return
    raise AssertionError("assert_allowed_cwd should have raised")


def test_assert_no_raise_on_accept():
    ca.assert_allowed_cwd(str(ROOT))  # should not raise


def test_nonexistent_subdir_under_allowlisted_root_is_allowed():
    # A project that hasn't been created yet should still pass the check.
    target = pathlib.Path.home() / "Desktop" / "fresh-project-12345"
    assert ca.is_allowed_cwd(str(target))


def test_symlink_outside_allowlist_is_rejected(tmp_path):
    # Build a symlink under /tmp pointing to /etc — resolve() should
    # flatten it, exposing /etc, which is outside the allowlist.
    link = tmp_path / "evil-link"
    try:
        link.symlink_to("/etc")
    except (OSError, NotImplementedError):
        return  # platforms where we can't symlink — skip
    # Add tmp_path to allowlist explicitly so we test the symlink-flattening,
    # not the parent-rejection.
    old = os.environ.get("JARVIS_EXTRA_PROJECT_DIRS")
    try:
        os.environ["JARVIS_EXTRA_PROJECT_DIRS"] = str(tmp_path)
        # tmp_path itself: allowed.
        assert ca.is_allowed_cwd(str(tmp_path / "real-dir"))
        # tmp_path/evil-link/<anything> resolves to /etc/<anything> — outside.
        assert not ca.is_allowed_cwd(str(link / "passwd"))
    finally:
        if old is None:
            os.environ.pop("JARVIS_EXTRA_PROJECT_DIRS", None)
        else:
            os.environ["JARVIS_EXTRA_PROJECT_DIRS"] = old
