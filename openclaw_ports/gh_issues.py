"""
GitHub issues helper: list + create.

Ported from openclaw/skills/gh-issues/SKILL.md at commit
125d82cab2952f87f532106a368d54e526141026.
MIT-licensed; see openclaw_ports/NOTICE.md for full license text.

Resync policy: manual diff against the pinned commit. Bump SHA in
NOTICE.md when forward-porting upstream changes.

The OpenClaw skill describes a comprehensive issue-to-PR workflow
(watch loops, PR creation, review handling, fork patterns). JARVIS
ports only the minimum useful slice: list open issues + create a
new issue. Both as plain HTTPS to api.github.com — no `gh` CLI
dependency, no subprocess. Token comes from the vault.
"""

from __future__ import annotations

import httpx

_API_BASE = "https://api.github.com"


class GhIssuesError(RuntimeError):
    """Raised on missing token, network failure, or non-2xx GitHub response."""


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def list_open_issues(owner_repo: str, token: str, limit: int = 10) -> list[dict]:
    """List open issues on `owner/repo`. Returns up to `limit` entries.

    Each entry: {"number", "title", "labels": [str], "url", "created_at"}.

    Raises GhIssuesError on missing token, non-2xx response, or transport error.
    """
    if not token:
        raise GhIssuesError("token required: GITHUB_TOKEN not configured in vault")
    url = f"{_API_BASE}/repos/{owner_repo}/issues"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            r = await http.get(
                url,
                headers=_auth_headers(token),
                params={"state": "open", "per_page": limit},
            )
    except httpx.HTTPError as e:
        raise GhIssuesError(f"network error: {e}") from e
    if r.status_code != 200:
        raise GhIssuesError(
            f"GitHub returned {r.status_code} on list_open_issues({owner_repo})"
        )
    raw = r.json()
    return [
        {
            "number": item["number"],
            "title": item["title"],
            "labels": [lbl["name"] for lbl in item.get("labels", [])],
            "url": item["html_url"],
            "created_at": item["created_at"],
        }
        for item in raw
        # Filter out pull requests (GitHub's /issues endpoint returns PRs too).
        if "pull_request" not in item
    ]


async def create_issue(
    owner_repo: str, title: str, body: str, token: str
) -> dict:
    """Create a new issue. Returns {"number", "url"} on success.

    Raises GhIssuesError on missing token or non-2xx response.
    """
    if not token:
        raise GhIssuesError("token required: GITHUB_TOKEN not configured in vault")
    url = f"{_API_BASE}/repos/{owner_repo}/issues"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            r = await http.post(
                url,
                headers=_auth_headers(token),
                json={"title": title, "body": body},
            )
    except httpx.HTTPError as e:
        raise GhIssuesError(f"network error: {e}") from e
    if r.status_code not in (200, 201):
        raise GhIssuesError(
            f"GitHub returned {r.status_code} on create_issue({owner_repo})"
        )
    data = r.json()
    return {"number": data["number"], "url": data["html_url"]}
