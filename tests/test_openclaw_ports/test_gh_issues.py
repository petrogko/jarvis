"""
Hermetic tests for openclaw_ports.gh_issues.

Mocks httpx so tests run without network. The module is a thin async
client over the GitHub REST API. A live integration test (real network,
real PAT) would live under tests/test_openclaw_ports/integration/ —
not in scope for this port.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from openclaw_ports import gh_issues


def test_attribution_header_present():
    """Umbrella spec §4.2 — per-file preamble points to upstream + SHA."""
    src = (ROOT / "openclaw_ports" / "gh_issues.py").read_text(encoding="utf-8")
    assert "Ported from openclaw/skills/gh-issues" in src
    assert "125d82cab2952f87f532106a368d54e526141026" in src
    assert "MIT-licensed" in src
    assert "openclaw_ports/NOTICE.md" in src


def test_module_surface():
    assert callable(gh_issues.list_open_issues)
    assert callable(gh_issues.create_issue)
    assert issubclass(gh_issues.GhIssuesError, Exception)


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *responses: _FakeResponse):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return self._responses.pop(0)

    async def post(self, url, headers=None, json=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self._responses.pop(0)


async def test_list_open_issues_happy_path(monkeypatch):
    payload = [
        {"number": 1, "title": "Bug 1", "html_url": "https://github.com/o/r/issues/1",
         "labels": [{"name": "bug"}], "created_at": "2026-05-01T00:00:00Z"},
        {"number": 2, "title": "Feature", "html_url": "https://github.com/o/r/issues/2",
         "labels": [], "created_at": "2026-05-02T00:00:00Z"},
    ]
    fake = _FakeAsyncClient(_FakeResponse(200, payload))
    monkeypatch.setattr(gh_issues.httpx, "AsyncClient", lambda **kw: fake)

    issues = await gh_issues.list_open_issues("o/r", token="ghp_xxx", limit=10)

    assert len(issues) == 2
    assert issues[0] == {"number": 1, "title": "Bug 1",
                        "labels": ["bug"],
                        "url": "https://github.com/o/r/issues/1",
                        "created_at": "2026-05-01T00:00:00Z"}
    assert fake.calls[0]["url"] == "https://api.github.com/repos/o/r/issues"
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer ghp_xxx"
    assert fake.calls[0]["params"] == {"state": "open", "per_page": 10}


async def test_list_open_issues_missing_token():
    with pytest.raises(gh_issues.GhIssuesError, match="token"):
        await gh_issues.list_open_issues("o/r", token="", limit=10)


async def test_list_open_issues_404(monkeypatch):
    fake = _FakeAsyncClient(_FakeResponse(404, {"message": "Not Found"}))
    monkeypatch.setattr(gh_issues.httpx, "AsyncClient", lambda **kw: fake)
    with pytest.raises(gh_issues.GhIssuesError, match="404"):
        await gh_issues.list_open_issues("o/r", token="ghp_xxx")


async def test_create_issue_happy_path(monkeypatch):
    payload = {"number": 42, "html_url": "https://github.com/o/r/issues/42"}
    fake = _FakeAsyncClient(_FakeResponse(201, payload))
    monkeypatch.setattr(gh_issues.httpx, "AsyncClient", lambda **kw: fake)

    result = await gh_issues.create_issue(
        "o/r", title="hi", body="body", token="ghp_xxx"
    )

    assert result == {"number": 42, "url": "https://github.com/o/r/issues/42"}
    assert fake.calls[0]["method"] == "POST"
    assert fake.calls[0]["url"] == "https://api.github.com/repos/o/r/issues"
    assert fake.calls[0]["json"] == {"title": "hi", "body": "body"}


async def test_create_issue_missing_token():
    with pytest.raises(gh_issues.GhIssuesError, match="token"):
        await gh_issues.create_issue("o/r", title="t", body="b", token="")
