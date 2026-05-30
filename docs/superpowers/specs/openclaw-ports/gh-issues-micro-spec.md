# `gh_issues` Port — Micro-Spec

**Wave-1 port 2** per `docs/superpowers/specs/2026-05-25-openclaw-ports-design.md` (umbrella).
**Date:** 2026-05-25
**Persona routing:** `security-advisor` (new network egress to `api.github.com`, new vault secret `GITHUB_TOKEN`) → implementation → `code-reviewer` → `test-runner`.

---

## 1. Upstream source

- **Path:** `openclaw/skills/gh-issues/SKILL.md`
- **Type:** Markdown LLM-instruction skill (not executable code). Documents an issue-to-PR automation workflow.
- **Ported at SHA:** `125d82cab2952f87f532106a368d54e526141026`

## 2. Scope (deliberate slice of the upstream skill)

OpenClaw's gh-issues skill is heavyweight: issue-to-PR automation, watch loops, PR-review handling, fork-vs-source PRs, cron mode. We port the **minimum useful slice** for JARVIS:

- **`[ACTION:GH_ISSUES_LIST owner/repo]`** — list open issues, return a butler-tone summary (count + top 3 with title/labels)
- **`[ACTION:GH_ISSUE_CREATE owner/repo|title|body]`** — create a new issue, return the issue URL

**Out of scope for this port:** watching, cron, PR creation, PR review handling, label/milestone/assignee filters (can ship later if used; not needed for the first cut).

## 3. Files

- Create: `openclaw_ports/gh_issues.py` (~150 LOC)
- Create: `tests/test_openclaw_ports/test_gh_issues.py` (hermetic, mocks httpx)
- Modify: `openclaw_ports/NOTICE.md` (add row to per-port table)
- Modify: `server.py` (extend `allowed` set with `GITHUB_TOKEN`; add `[ACTION:GH_ISSUES_LIST]` / `[ACTION:GH_ISSUE_CREATE]` handlers in the action dispatch loop; add `github_token` to `/api/settings/preferences` response)
- Modify: `frontend/src/settings.ts` (new GitHub Token password input; extend `PreferencesResponse` interface)
- Modify: `docs/BACKLOG.md` (mark port 2 done after PR merges)

## 4. Python deps

None new. Use existing `httpx` (already in `requirements.txt`).

## 5. Module API

```python
# openclaw_ports/gh_issues.py

class GhIssuesError(RuntimeError):
    """Raised on missing token, network failure, or non-2xx GitHub response."""


async def list_open_issues(owner_repo: str, token: str, limit: int = 10) -> list[dict]:
    """Return list of {number, title, labels, url, created_at} dicts.

    Raises GhIssuesError on missing token or non-2xx response.
    """


async def create_issue(owner_repo: str, title: str, body: str, token: str) -> dict:
    """Create a new issue. Return {number, url} on success.

    Raises GhIssuesError on missing token or non-2xx response.
    """
```

## 6. Vault secret

- New vault key: `GITHUB_TOKEN`
- Allowlist extension in `server.py:_settings_keys.allowed`
- UI: new password input in `frontend/src/settings.ts` API Keys section, labeled "GitHub Token", placeholder "ghp_... or github_pat_..."
- Stored alongside other secrets (SQLCipher); read at action-handler time via `_vault_get("GITHUB_TOKEN")`

## 7. Security checks

- **Egress:** new host `api.github.com`. Update `docs/DOCKER.md` egress allowlist table.
- **Token format:** accept `ghp_*` (classic PAT) and `github_pat_*` (fine-grained). Don't validate format; just pass-through the Authorization header.
- **Untrusted content:** issue titles + bodies are user-controlled but come from GitHub which is a trusted-enough source for read operations. Still: when JARVIS quotes issue content back to the user, route through `untrusted_content.sanitize` + `wrap` (consistent with the mail/calendar pattern).
- **Rate limiting:** GitHub returns 429/secondary-rate-limit headers; surface those as `GhIssuesError` with the GitHub-provided retry-after; don't auto-retry.

## 8. JARVIS integration points

In `server.py` action dispatch loop (search for `[ACTION:BROWSE]` / `[ACTION:RESEARCH]` handlers as reference):

```python
elif action_type == "GH_ISSUES_LIST":
    owner_repo = action_arg.strip()
    from openclaw_ports import gh_issues
    try:
        issues = await gh_issues.list_open_issues(
            owner_repo, _vault_get("GITHUB_TOKEN"), limit=10
        )
    except gh_issues.GhIssuesError as e:
        # speak the error back via the regular response path
        ...
```

Same shape for `GH_ISSUE_CREATE` with `owner/repo|title|body` arg format.

System prompt update (`JARVIS_SYSTEM_PROMPT` in server.py): add two lines under the action-tag list:
- `[ACTION:GH_ISSUES_LIST owner/repo]` — list open issues on a GitHub repo
- `[ACTION:GH_ISSUE_CREATE owner/repo|title|body]` — open a new GitHub issue

## 9. Test coverage

Hermetic tests in `tests/test_openclaw_ports/test_gh_issues.py`:
- `test_list_open_issues_happy_path` — mock httpx, assert returned dicts have the expected keys
- `test_list_open_issues_missing_token` — assert `GhIssuesError` raised
- `test_list_open_issues_404` — assert `GhIssuesError` raised on 404
- `test_create_issue_happy_path` — mock httpx, assert POST body shape + returned URL
- `test_create_issue_missing_token` — assert `GhIssuesError`
- `test_attribution_header_present` — same pattern as tts_local_cli (skill SHA in module docstring)

## 10. Acceptance criterion

End-to-end: with a real `GITHUB_TOKEN` saved in the vault, "JARVIS, list issues on petrogko/jarvis" produces a spoken summary like "Five open issues, sir. Top one: <title>."

## 11. Out-of-scope for v1 (future enhancements)

- PR creation flow
- Issue update / close
- Watch / poll for new issues
- Label / assignee / milestone filters
- Fork-to-source PR pattern

These can be added incrementally without re-spec'ing — each as a new `[ACTION:GH_*]` handler reading the same vault token.
