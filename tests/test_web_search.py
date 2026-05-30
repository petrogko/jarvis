"""Hermetic tests for openclaw_ports.web_search (Tavily)."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openclaw_ports import web_search


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self._response


async def test_search_happy_path_returns_normalized_results(monkeypatch):
    fake = _FakeClient(_FakeResp(200, {
        "answer": "British neural TTS via Piper.",
        "results": [
            {"title": "Piper", "url": "https://github.com/OHF-Voice/piper1-gpl",
             "content": "GPL-3.0 neural TTS.", "score": 0.91},
            {"title": "Voices", "url": "https://huggingface.co/rhasspy/piper-voices",
             "content": "Pretrained voices.", "score": 0.84},
        ],
    }))
    monkeypatch.setattr(web_search.httpx, "AsyncClient", lambda **kw: fake)

    out = await web_search.search("piper neural TTS", token="tvly-xyz")
    assert out["answer"] == "British neural TTS via Piper."
    assert len(out["results"]) == 2
    assert out["results"][0]["title"] == "Piper"
    assert out["results"][0]["url"].startswith("https://")

    call = fake.calls[0]
    assert call["url"] == "https://api.tavily.com/search"
    assert call["headers"]["Authorization"] == "Bearer tvly-xyz"
    assert call["json"]["query"] == "piper neural TTS"
    assert call["json"]["max_results"] == 5  # default


async def test_search_passes_optional_filters(monkeypatch):
    fake = _FakeClient(_FakeResp(200, {"results": []}))
    monkeypatch.setattr(web_search.httpx, "AsyncClient", lambda **kw: fake)

    await web_search.search(
        "weather london",
        token="tvly-x",
        max_results=8,
        search_depth="advanced",
        topic="news",
        include_answer=True,
        time_range="week",
        include_domains=["bbc.co.uk"],
        exclude_domains=["spam.example"],
    )
    body = fake.calls[0]["json"]
    assert body["max_results"] == 8
    assert body["search_depth"] == "advanced"
    assert body["topic"] == "news"
    assert body["include_answer"] is True
    assert body["time_range"] == "week"
    assert body["include_domains"] == ["bbc.co.uk"]
    assert body["exclude_domains"] == ["spam.example"]


async def test_search_empty_query_raises(monkeypatch):
    with pytest.raises(web_search.WebSearchError, match="empty"):
        await web_search.search("", token="tvly-x")
    with pytest.raises(web_search.WebSearchError, match="empty"):
        await web_search.search("   ", token="tvly-x")


async def test_search_missing_token_raises():
    with pytest.raises(web_search.WebSearchError, match="token required"):
        await web_search.search("anything", token="")


async def test_search_invalid_enums_raise():
    with pytest.raises(web_search.WebSearchError, match="search_depth"):
        await web_search.search("x", token="tvly-x", search_depth="extreme")
    with pytest.raises(web_search.WebSearchError, match="topic"):
        await web_search.search("x", token="tvly-x", topic="celebrity")
    with pytest.raises(web_search.WebSearchError, match="time_range"):
        await web_search.search("x", token="tvly-x", time_range="century")


async def test_search_clamps_max_results(monkeypatch):
    fake = _FakeClient(_FakeResp(200, {"results": []}))
    monkeypatch.setattr(web_search.httpx, "AsyncClient", lambda **kw: fake)
    await web_search.search("x", token="tvly-x", max_results=999)
    assert fake.calls[0]["json"]["max_results"] == 20  # capped at 20
    await web_search.search("x", token="tvly-x", max_results=0)
    assert fake.calls[1]["json"]["max_results"] == 1  # floored at 1


async def test_search_non_200_raises(monkeypatch):
    fake = _FakeClient(_FakeResp(401, {"detail": "bad key"}))
    monkeypatch.setattr(web_search.httpx, "AsyncClient", lambda **kw: fake)
    with pytest.raises(web_search.WebSearchError, match="401"):
        await web_search.search("x", token="tvly-x")


async def test_search_network_error_raises(monkeypatch):
    class _Boom:
        async def __aenter__(self): raise web_search.httpx.ConnectError("nope")
        async def __aexit__(self, *e): pass
    monkeypatch.setattr(web_search.httpx, "AsyncClient", lambda **kw: _Boom())
    with pytest.raises(web_search.WebSearchError, match="network error"):
        await web_search.search("x", token="tvly-x")
