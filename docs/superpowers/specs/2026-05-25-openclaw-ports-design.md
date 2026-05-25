# OpenClaw Ports — Umbrella Design

**Status:** design (for review)
**Date:** 2026-05-25
**Backlog item:** supersedes P11 in `docs/BACKLOG.md`
**Persona routing:** `software-architect` validated the umbrella scope via brainstorming → `security-advisor` MUST review per-port secret/network surface as each micro-spec is written → `code-reviewer` before commit on every port → `test-runner` before any "ready to merge" claim. Memory-lancedb gets its OWN architect-led brainstorm before its micro-spec (see §10).

---

## 1. Goals

1. **Selectively absorb OpenClaw extension behavior into JARVIS** as native Python modules. OpenClaw (https://github.com/openclaw/openclaw, MIT) is a mature TypeScript multi-channel assistant framework with 136 extensions; JARVIS is a voice-first single-user macOS butler. We port the implementations we want without coupling JARVIS to OpenClaw's runtime.
2. **Preserve JARVIS's identity and architecture.** Vault, lock-screen, persona system, `[ACTION:X]` dispatch, butler prompt, and all 8 prior hardening PRs stay intact. No OpenClaw process runs alongside JARVIS by default.
3. **Make license compliance auditable.** MIT requires attribution, not isolation. Every ported file points back to its OpenClaw source path and commit SHA. One umbrella NOTICE.md carries the full license text.
4. **Ship the first port this session** as proof the convention works, and to validate the porting cost is genuinely small for the candidates we audited.
5. **Define an escape hatch** for future extensions where porting cost is prohibitive (Node-native deps, complex SDKs). Documented but unused in wave 1.

## 2. Non-goals

- A general-purpose JS↔Python bridge framework. The escape hatch (§7) is intentionally minimal — only built if and when a specific port needs it.
- Adopting OpenClaw's plugin loader, gateway protocol, or extension lifecycle. JARVIS is not a client of OpenClaw's runtime.
- Multi-channel support (WhatsApp, Telegram, Slack, etc.). Those OpenClaw extensions are out of scope; JARVIS remains voice-first single-user.
- Re-implementing OpenClaw's own auth/secret model. JARVIS's vault is canonical.
- Automated upstream-resync tooling. With ~5 ports in wave 1, manual diff against a pinned commit is cheaper than building sync automation.

## 3. Architecture

### 3.1 Directory layout

```
openclaw_ports/
├── NOTICE.md                      # MIT license text + per-port attribution table
├── __init__.py                    # empty
├── tts_local_cli.py               # wave 1 — port 1
├── healthcheck.py                 # wave 1 — port 2
├── apple_notes.py                 # wave 1 — port 3
├── gh_issues.py                   # wave 1 — port 4
├── memory_lancedb.py              # wave 1 — port 5 (deferred behind own architect spec)
└── _subprocess_bridge.py          # escape hatch; created lazily on first need
```

- **One module per port.** Snake-case from the kebab-case extension name. No nested packages — keeps imports simple (`from openclaw_ports import tts_local_cli`).
- **No re-implementation of OpenClaw's plugin gateway.** Each ported module exposes a clean Python API (e.g., `tts_local_cli.synthesize(text, voice="alex") -> bytes`). JARVIS calls these like any other internal module.

### 3.2 Integration into JARVIS

Existing JARVIS call sites import the port directly:

```python
# server.py — old:
async def synthesize_speech(text: str) -> bytes:
    # ... Fish Audio HTTPS call ...

# server.py — new (after wave 1 port 1):
from openclaw_ports import tts_local_cli
async def synthesize_speech(text: str) -> bytes:
    return await tts_local_cli.synthesize(text, voice=_vault_get("TTS_VOICE", "alex"))
```

No new dispatch layer. No subprocess. No IPC. Just a module import.

### 3.3 Trust boundary

Each ported module that touches:
- A secret → registers a key in the vault `secrets` allowlist (extends `server.py:_settings_keys.allowed`).
- The network → goes through `httpx` with explicit base URLs (no auto-discovery). Egress hosts documented in the micro-spec.
- AppleScript → uses the existing `actions.run_osascript` argv-pass helper. Never f-string interpolation. (Same rule as the rest of JARVIS.)
- Any input that flows into the LLM context → passes through `untrusted_content.sanitize` + `wrap`.

These rules are enforced by code-review on each micro-spec, not by infrastructure. The umbrella spec calls them out so the rules are visible.

## 4. Attribution & licensing

### 4.1 NOTICE.md format

```
# OpenClaw Ports — Attribution

Modules in this directory are ported from OpenClaw
(https://github.com/openclaw/openclaw), MIT-licensed.

## Pinned upstream commit
<SHA at time of first port>

## Per-port table
| Module                  | Upstream path                                 | Ported at SHA | Last resync |
|-------------------------|-----------------------------------------------|---------------|-------------|
| tts_local_cli.py        | extensions/tts-local-cli/src/                 | <SHA>         | <date>      |
| healthcheck.py          | skills/healthcheck/                           | <SHA>         | <date>      |
| ...                     |                                               |               |             |

## MIT License (verbatim)
<full upstream LICENSE text>
```

### 4.2 Per-file header

Every ported file starts with exactly this preamble (replace placeholders):

```python
"""
<one-line description of the module>

Ported from openclaw/<upstream-path> at commit <SHA>.
MIT-licensed; see openclaw_ports/NOTICE.md for full license text.

Resync policy: manual diff against the pinned commit. Bump SHA in
NOTICE.md when forward-porting upstream changes.
"""
```

No exceptions. The `code-reviewer` persona will reject files missing the preamble.

### 4.3 Upstream-resync workflow

When we want to absorb an OpenClaw upgrade for a specific port:

1. Look up the current ported SHA in NOTICE.md.
2. `cd /Users/petrog/Development/github/openclaw && git diff <old_sha> HEAD -- <upstream-path>` — read the diff.
3. Forward-port the changes by hand. Run the port's tests. Commit with a `chore(openclaw_ports): resync <name> to <new_sha>` message.
4. Update NOTICE.md with the new SHA and resync date.

No automation. We expect to do this maybe once a year per port.

## 5. Test convention

### 5.1 Hermetic tests

- Live at `tests/test_openclaw_ports/test_<module_name>.py`.
- Mock all external surfaces — `osascript`, HTTP clients, subprocess calls. Existing JARVIS tests show the pattern (`test_applescript_safety.py`, `test_untrusted_content.py`).
- Run on every CI invocation (included in default pytest collection).
- Each port MUST ship with hermetic coverage of: happy path, one failure mode, one adversarial input (where applicable).

### 5.2 Live integration tests (optional)

- Live at `tests/test_openclaw_ports/integration/test_<name>_live.py`.
- Marked `@pytest.mark.integration` and added to `pyproject.toml`'s `addopts` `--ignore` list, same as `test_classifier.py` and `test_goal_drift.py`.
- Hit real external APIs (GitHub for gh_issues, system TTS for tts_local_cli). Run manually with explicit `pytest tests/test_openclaw_ports/integration/test_<name>_live.py`.
- NOT required for merge; nice-to-have for confidence.

## 6. Secret integration

Ports needing secrets:

1. Extend the `allowed` set at `server.py:_settings_keys` (line ~2625 today).
2. Document the secret in `SECURITY.md`'s data-classification table — new row, at-rest column = `data/secrets.db (SQLCipher)`.
3. Add the corresponding input to the UI settings panel in `frontend/src/settings.ts`. The micro-spec specifies the label text.
4. Use `vault.session().settings.get("<KEY>")` to read at request time. No module-level constants.

The `security-advisor` persona reviews each new secret before the micro-spec lands.

## 7. Subprocess escape hatch (`_subprocess_bridge.py`)

**Not built in wave 1.** Designed here so it's not improvised later.

When porting a specific OpenClaw extension is impractical (e.g., a Node-native dep with no good Python equivalent, or a large body of code where porting cost exceeds 200 LOC), we may instead spawn the OpenClaw extension as a subprocess and communicate over stdin/stdout JSON-RPC.

Minimum acceptable shape:

```python
# openclaw_ports/_subprocess_bridge.py

async def call_extension(extension_path: str, fn: str, args: dict) -> dict:
    """Spawn the OpenClaw extension via `pnpm exec ... --json-rpc-stdio`,
    send a single request, await a single response, kill the child.

    extension_path: path under the OpenClaw repo (e.g., 'extensions/<name>').
    fn:             the extension's exported function name.
    args:           JSON-serializable dict.

    Returns the JSON-decoded response dict.

    Spawned with: --max-old-space-size=256, --no-experimental-fetch,
    nice -n 10, cwd=OpenClaw repo root, env minimized to what the
    extension declares.
    """
```

Caveats:
- Requires a working Node + pnpm install of OpenClaw at a documented path. Operators who don't have OpenClaw cloned cannot use bridge-based ports.
- Per-call cost: 200–800ms startup. Acceptable for non-voice paths (e.g., a research action). UNACCEPTABLE inside the voice loop.
- Security: subprocess inherits no env vars beyond an allowlist; output sanitized through `untrusted_content` before any LLM context use.

This file does NOT get created until a port explicitly needs it. Its existence in the spec is to prevent improvisation later.

## 8. Per-port micro-spec checklist

Every micro-spec under `docs/superpowers/specs/openclaw-ports/<name>.md` MUST include:

1. **Upstream source path** in OpenClaw + **commit SHA** at time of port.
2. **Lines of code** to port (approximate).
3. **External dependencies** — Python equivalents for each OpenClaw Node dep.
4. **Secret requirements** — vault keys needed; UI input labels.
5. **JARVIS integration point** — exact file:line where the new function gets called.
6. **Test coverage plan** — hermetic + optional integration; list each test name.
7. **Acceptance criterion** — one concrete behavior that proves the port works end-to-end.
8. **Out-of-scope features** — what we deliberately omit from the OpenClaw original (e.g., multi-channel routing for tts-local-cli; we only need the macOS `say` path).

The `software-architect` persona reviews each micro-spec before the implementation plan is written.

## 9. Implementation order

**Wave 1 (this umbrella's scope) — 4 ports, sequential, one PR each:**

1. **tts_local_cli** — port first. Smallest blast radius. Directly solves backlog P3 (eliminate Fish Audio). Validates the porting + attribution + test conventions on a small target. ~50 LOC.
2. **healthcheck** — fixes the connection-status panel that currently shows "port undefined | up NaNh NaNm" and red dots. Pure read-only port. ~80 LOC.
3. **apple_notes** — first port that uses AppleScript (host-mode only). Proves the `run_osascript` argv-pass integration. ~100 LOC. New `[ACTION:READ_NOTE]` / `[ACTION:CREATE_NOTE]` (already exists; this expands it).
4. **gh_issues** — first port that uses a network secret. Proves the vault-secret pattern end-to-end (new vault key, UI input, security-advisor sign-off). ~120 LOC. New `[ACTION:GH_ISSUES_LIST]`, `[ACTION:GH_ISSUE_CREATE]`.

**Wave 2 (separate spec, separate brainstorm, separate umbrella):**

5. **memory_lancedb** — NOT in this umbrella. Requires its own `software-architect` brainstorm before any micro-spec is written. See §10 for why.

## 10. Memory-lancedb caveat (mandatory architect deferral)

Out of scope for THIS umbrella spec. Reasons:

- **New dependency:** LanceDB is a Rust-backed vector DB. Adds substantial runtime weight + a Rust toolchain dep at install time. Worth its own audit.
- **Embedding model decision:** are we using Anthropic embeddings (network, privacy concern) or a local model like `sentence-transformers/all-MiniLM-L6-v2` (CPU cost, ~80 MB download)? This is a privacy + cost tradeoff, not a porting decision.
- **Schema change:** semantic memory needs an `embedding BLOB` column on the memory table. The vault currently holds memory in SQLCipher. Either we put LanceDB inside the vault (encrypted blob in the DB) or alongside it (separate file, different encryption story). Architect call.
- **Recall re-architecture:** `memory.py`'s current FTS5-only recall would gain a hybrid embedding+FTS path. Touches the recall scoring + the `[ACTION:REMEMBER]` / recall surface.

When ready: open a separate brainstorm for `memory-lancedb` — that one IS an architectural change, not a port. The micro-spec for that port follows the brainstorm.

## 11. Documentation updates required

Bundled with wave-1 port 1 (`tts_local_cli`):

- `SECURITY.md` — note that ports are MIT-licensed and live under `openclaw_ports/`. No threat model change for the TTS port (replaces Fish Audio with macOS `say`; removes a third-party egress).
- `ARCHITECTURE.md` — add `openclaw_ports/` row to the module map: "Ported MIT-licensed implementations from OpenClaw upstream; see `openclaw_ports/NOTICE.md`."
- `docs/BACKLOG.md` — supersede P11 with this umbrella; add follow-up entries for each remaining port.
- `CLAUDE.md` — add one line to persona routing: "Editing any file under `openclaw_ports/` → invoke `code-reviewer` to verify attribution header is present and NOTICE.md is up to date."

## 12. Open questions

None blocking the umbrella. The questions below get answered inside each port's micro-spec:

- For `tts_local_cli`: which macOS voices do we expose by default? (Probably "Alex" + "Samantha"; user-configurable via vault `TTS_VOICE` key.)
- For `healthcheck`: what does "connection status" mean for the dots — host-only AppleScript reachability, vault state, vault session age, container resource pressure? Micro-spec decides.
- For `apple_notes`: is OpenClaw's implementation more sophisticated than JARVIS's existing `notes_access.py`? If so, do we replace or augment? Micro-spec compares.
- For `gh_issues`: do we need GitHub App auth, fine-grained PAT, or classic PAT? OpenClaw probably uses the most flexible; we mirror their choice.

## 13. Out-of-scope follow-ups

- Subprocess escape hatch implementation (deferred until needed).
- Memory-lancedb port (its own brainstorm).
- Multi-channel OpenClaw extensions (Telegram, Discord, etc.) — not aligned with voice-first single-user JARVIS.
- Automated upstream-resync tooling.
- Adopting OpenClaw's plugin SDK or gateway protocol.
