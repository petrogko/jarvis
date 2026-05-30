"""
Web search via Tavily.

Ported from openclaw/extensions/tavily at commit
125d82cab2952f87f532106a368d54e526141026.
MIT-licensed; see openclaw_ports/NOTICE.md for full license text.

Resync policy: manual diff against the pinned commit. Bump SHA in
NOTICE.md when forward-porting upstream changes.

The OpenClaw extension wraps Tavily Search + Extract behind a richer
provider/tool interface tied to the OpenClaw plugin SDK. JARVIS ports
only the minimum useful slice: a single `search(query, ...)` coroutine
that POSTs to https://api.tavily.com/search and returns a normalized
list of results. No `extract`, no cache, no schema validation — those
can be added later as separate ports. Token comes from the vault key
TAVILY_API_KEY.
"""

from __future__ import annotations

import httpx

_API_URL = "https://api.tavily.com/search"
_MAX_RESULTS_CAP = 20
_DEFAULT_RESULTS = 5
_VALID_SEARCH_DEPTHS = ("basic", "advanced")
_VALID_TOPICS = ("general", "news", "finance")
_VALID_TIME_RANGES = ("day", "week", "month", "year")


class WebSearchError(RuntimeError):
    """Raised on missing token, non-2xx response, or transport error."""


async def search(
    query: str,
    token: str,
    *,
    max_results: int = _DEFAULT_RESULTS,
    search_depth: str | None = None,
    topic: str | None = None,
    include_answer: bool = False,
    time_range: str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    timeout_s: float = 15.0,
) -> dict:
    """Search the web via Tavily. Returns {"answer": str|None, "results": [...]}.

    Each result dict has: title, url, content, score.

    Raises WebSearchError on missing token, non-2xx response, transport error,
    or invalid enum value.
    """
    if not query or not query.strip():
        raise WebSearchError("query is empty")
    if not token:
        raise WebSearchError("token required: TAVILY_API_KEY not configured in vault")

    if search_depth is not None and search_depth not in _VALID_SEARCH_DEPTHS:
        raise WebSearchError(f"invalid search_depth: {search_depth!r}")
    if topic is not None and topic not in _VALID_TOPICS:
        raise WebSearchError(f"invalid topic: {topic!r}")
    if time_range is not None and time_range not in _VALID_TIME_RANGES:
        raise WebSearchError(f"invalid time_range: {time_range!r}")

    count = max(1, min(_MAX_RESULTS_CAP, int(max_results)))

    body: dict = {"query": query, "max_results": count}
    if search_depth:
        body["search_depth"] = search_depth
    if topic:
        body["topic"] = topic
    if include_answer:
        body["include_answer"] = True
    if time_range:
        body["time_range"] = time_range
    if include_domains:
        body["include_domains"] = include_domains
    if exclude_domains:
        body["exclude_domains"] = exclude_domains

    # Tavily accepts the API key as `Authorization: Bearer <key>`.
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as http:
            r = await http.post(_API_URL, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise WebSearchError(f"network error: {e}") from e

    if r.status_code != 200:
        raise WebSearchError(
            f"Tavily returned {r.status_code} on search({query!r:.80})"
        )

    payload = r.json()
    results = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "content": item.get("content", ""),
            "score": item.get("score"),
        }
        for item in payload.get("results", [])
    ]
    return {
        "answer": payload.get("answer"),
        "results": results,
    }
