"""
Tests for jarvis_sidecar.cwd_allowlist — the structural guard between
JARVIS-in-Docker and `claude --dangerously-skip-permissions` on the host.

Isolation: HOME is monkeypatched to tmp_path per test so the allowlist
honors a synthetic ~/Desktop and never touches the operator's real one.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis_sidecar import cwd_allowlist


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Synthetic $HOME. Pre-creates ~/Desktop so the default allowlist is real."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads $HOME on POSIX; pathlib also honors $USERPROFILE on Win
    # but we're macOS/Linux for the sidecar.
    monkeypatch.delenv("JARVIS_EXTRA_PROJECT_DIRS", raising=False)
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    yield tmp_path


# --- Happy path -------------------------------------------------------------

def test_workdir_under_default_desktop_is_allowed(home):
    project = home / "Desktop" / "my-app"
    project.mkdir()
    ok, reason = cwd_allowlist.check_workdir(str(project))
    assert ok, reason
    cwd_allowlist.assert_allowed_workdir(str(project))  # no raise


def test_workdir_under_extra_dir_is_allowed(home, monkeypatch):
    extra = home / "WorkProjects"
    extra.mkdir()
    project = extra / "thing"
    project.mkdir()
    monkeypatch.setenv("JARVIS_EXTRA_PROJECT_DIRS", str(extra))
    ok, reason = cwd_allowlist.check_workdir(str(project))
    assert ok, reason


# --- Rejection: outside allowlist ------------------------------------------

def test_workdir_outside_allowlist_rejected(home):
    other = home / "Documents"
    other.mkdir()
    ok, reason = cwd_allowlist.check_workdir(str(other))
    assert not ok
    assert "outside allowlist" in reason


def test_etc_rejected(home):
    ok, reason = cwd_allowlist.check_workdir("/etc")
    assert not ok


def test_path_traversal_flattened_and_rejected(home):
    # ~/Desktop/../.. — resolves to a parent of HOME and not under any root.
    ok, reason = cwd_allowlist.check_workdir(str(home / "Desktop" / ".." / ".."))
    assert not ok


# --- Rejection: hard-deny list ---------------------------------------------

def test_home_root_itself_rejected(home, monkeypatch):
    """Even if an operator misconfigures EXTRA_PROJECT_DIRS=~, deny."""
    monkeypatch.setenv("JARVIS_EXTRA_PROJECT_DIRS", str(home))
    ok, reason = cwd_allowlist.check_workdir(str(home))
    assert not ok
    assert "denied" in reason.lower()


def test_library_rejected(home, monkeypatch):
    lib = home / "Library"
    lib.mkdir()
    monkeypatch.setenv("JARVIS_EXTRA_PROJECT_DIRS", str(lib))
    ok, reason = cwd_allowlist.check_workdir(str(lib))
    assert not ok


def test_ssh_rejected(home, monkeypatch):
    ssh = home / ".ssh"
    ssh.mkdir()
    monkeypatch.setenv("JARVIS_EXTRA_PROJECT_DIRS", str(ssh))
    ok, reason = cwd_allowlist.check_workdir(str(ssh))
    assert not ok


@pytest.mark.parametrize("name", [".aws", ".config", ".gnupg", ".kube", ".docker", ".git"])
def test_denied_component_rejected(home, monkeypatch, name):
    """A workdir containing any denied component is rejected even if its
    parent is allowlisted."""
    project = home / "Desktop" / "x" / name
    project.mkdir(parents=True)
    ok, reason = cwd_allowlist.check_workdir(str(project))
    assert not ok


def test_dotenv_component_rejected(home):
    project = home / "Desktop" / "app" / ".env"
    project.mkdir(parents=True)
    ok, reason = cwd_allowlist.check_workdir(str(project))
    assert not ok
    assert ".env" in reason


def test_dotenv_local_component_rejected(home):
    project = home / "Desktop" / "app" / ".env.local"
    project.mkdir(parents=True)
    ok, reason = cwd_allowlist.check_workdir(str(project))
    assert not ok


# --- Rejection: symlink as input -------------------------------------------

def test_symlink_input_rejected_even_if_target_allowed(home):
    target = home / "Desktop" / "real-project"
    target.mkdir()
    link = home / "Desktop" / "linked"
    link.symlink_to(target)
    ok, reason = cwd_allowlist.check_workdir(str(link))
    assert not ok
    assert "symlink" in reason


# --- Rejection: non-existent / non-directory -------------------------------

def test_nonexistent_path_rejected(home):
    project = home / "Desktop" / "never-created"
    ok, reason = cwd_allowlist.check_workdir(str(project))
    assert not ok
    assert "does not exist" in reason


def test_file_rejected_not_dir(home):
    f = home / "Desktop" / "file.txt"
    f.write_text("x")
    ok, reason = cwd_allowlist.check_workdir(str(f))
    assert not ok


# --- Empty / None ----------------------------------------------------------

def test_empty_rejected(home):
    ok, reason = cwd_allowlist.check_workdir("")
    assert not ok
    assert "empty" in reason
    ok, reason = cwd_allowlist.check_workdir(None)
    assert not ok


# --- assert_ wrapper raises ValueError -------------------------------------

def test_assert_raises_on_denied(home):
    with pytest.raises(ValueError, match="refusing workdir"):
        cwd_allowlist.assert_allowed_workdir("/etc")
