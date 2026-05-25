# UI-Only Configuration with Encrypted At-Rest Storage

**Status:** design (for review)
**Date:** 2026-05-24
**Backlog item:** P1 (merged with P2 — see [`docs/BACKLOG.md`](../../../docs/BACKLOG.md))
**Branches affected:** new `feat/ui-config-encrypted-2026-05`
**Persona routing:** `software-architect` validated this scope via brainstorming → `security-advisor` MUST review before implementation (touches secrets-at-rest, `auth.py`, the data-classification boundary in `SECURITY.md`).

---

## 1. Goals

1. **Remove `.env` as the canonical config store.** All API keys (Anthropic, Fish Audio), auth token, and user preferences (name, honorific, calendar accounts, voice ID) are managed via the existing UI settings panel and survive Docker container restarts cleanly.
2. **Encrypt secrets and memory at rest from day one.** No plaintext API keys, no plaintext memory database on disk.
3. **Isolate blast radius.** A bug in the memory subsystem cannot corrupt secrets, and vice versa. Two separate encrypted SQLite databases.
4. **Real encryption.** User passphrase required on every container start to unlock. No machine-bound key escape hatch (FileVault remains the laptop-loss defense; this is layered on top, not in place of).
5. **Safe migration.** Existing `data/jarvis.db` (plain SQLite today) and `.env` are imported on first successful unlock; originals are kept until import is verified.

## 2. Non-goals

- Host-only Python install (`python server.py` without Docker) is no longer a supported deployment per the brainstorming decision. Docker is the only target.
- Per-column envelope encryption (option C from brainstorming) — out of scope; SQLCipher whole-DB encryption is the chosen primitive.
- Multi-user / role-based access. JARVIS is single-user.
- Passphrase recovery via a "hint" field — explicitly deferred; can be added later as a non-secret column.
- Replacing the auth token model (JARVIS's existing token-based auth on `/api/*` is orthogonal to this work).

## 3. Storage layout

Two SQLCipher databases live under the existing bind-mounted `data/` directory:

| File | Purpose | Schema (load-bearing tables) |
|---|---|---|
| `data/secrets.db` | API keys, auth token, UI preferences | `secrets(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)` |
| `data/jarvis.db` | Conversation memory, tasks (replaces today's plain SQLite memory at the same path) | Existing schema from `memory.py` + tasks, ported under SQLCipher. |
| `data/audit.jsonl` | Audit log | **Stays plaintext, append-only** — preserves forensic recoverability when the passphrase is lost. See §11 SECURITY.md update. |

Both are encrypted with the same master key, derived from the user's passphrase by **Argon2id** (parameters specified in §6). The KDF salt is stored unencrypted at `data/kdf.salt` (16 bytes, random, generated on first boot). The salt being public is fine — Argon2id is salted-and-stretched; what protects the data is the passphrase, not the salt.

Why two files and not one (vs. brainstorming option A):
- A bug that corrupts a single SQLCipher journal can in the worst case take that DB with it. Separating secrets from memory means a memory write loop gone wrong cannot also lose your API keys.
- Different access patterns: secrets are written rarely (handful of times during setup) and read once per request. Memory is written constantly. Isolating them lets us add per-store backup or rotation policies later without redesigning.

Trade-off accepted: two opens, two unlocks, two close paths in the runtime. Encapsulated in a `vault.py` module that exposes both as one logical session (§4).

## 4. Module layout

New module:

- `vault.py` — the only place that touches SQLCipher. Public surface:
  - `vault.bootstrap(passphrase: str) -> None` — first-run; creates `kdf.salt`, derives key, creates empty `secrets.db` and `memory.db`, then runs `migrate_from_legacy()`.
  - `vault.unlock(passphrase: str) -> VaultSession` — derives the key from the passphrase + salt, opens both DBs, returns a session holding the live connections. Raises `VaultLockedError` on wrong passphrase.
  - `vault.session() -> VaultSession | None` — returns the current in-process unlocked session, or `None` if locked.
  - `vault.lock() -> None` — closes connections, drops in-process key material. Used on `/api/auth/lock` and on shutdown.
  - `vault.is_initialized() -> bool` — does `data/kdf.salt` exist? Determines lock-screen vs first-run flow.

Modified modules:

- `server.py` — replace `_read_env` / `_write_env_key` calls with `vault.session().settings.get/set`. Add a middleware that returns `423 Locked` on all `/api/*` (and `/ws/*`) except a small allowlist when `vault.session()` is `None`. Add three new endpoints:
  - `POST /api/auth/bootstrap` — first-run only; accepts a passphrase, calls `vault.bootstrap`.
  - `POST /api/auth/unlock` — accepts a passphrase, calls `vault.unlock`. Returns 200 on success, 401 on wrong passphrase.
  - `POST /api/auth/lock` — calls `vault.lock`. Returns 204.
- `memory.py` — connection-construction swap to use the SQLCipher connection from `vault.session().memory_conn`. No schema changes; same SQL. (`memory.py` no longer opens its own DB file.)
- `auth.py` — auth token now lives in `secrets.db` table not the `data/.local_token` file. The startup flow:
  1. Container starts, no vault unlocked.
  2. All `/api/*` return `423` except `GET /api/health`, `GET /api/auth/state`, `POST /api/auth/bootstrap`, `POST /api/auth/unlock`.
  3. First request after unlock: `auth.py` reads the token from `vault.session().secrets`. If missing, generates one and writes back.
  4. `auth.py:_PUBLIC_PATHS` MUST be extended to include the four auth endpoints (current `_PUBLIC_PATHS` at `auth.py:33` rejects unauthenticated requests; without this fix the unlock endpoint is unreachable).
  5. **The token does not survive container restart unless the vault is unlocked again** — operator tooling that reads `data/.local_token` directly stops working. Documented in §11 SECURITY.md update.

Removed surface:

- `.env` is no longer read by the server at request time. Compose still allows `.env` to be present as a **bootstrap convenience** (auto-imported on first unlock), but after migration it's deleted.
- `_read_env` / `_write_env_key` in `server.py` are removed; their call sites move to `vault.session().settings`.
- The `auth_token` file at `data/auth_token` is migrated into `secrets.db` then deleted.

## 5. Endpoints

| Endpoint | When | Auth | Allowed while locked? |
|---|---|---|---|
| `GET /api/health` | Always | None | Yes |
| `GET /api/auth/state` | Always | None | Yes — returns `{ initialized, locked }` so the UI knows which screen to show |
| `POST /api/auth/bootstrap` | First run only | None | Yes (only when `!initialized`) |
| `POST /api/auth/unlock` | Container start, lock-screen | None | Yes (only when `initialized && locked`) |
| `POST /api/auth/lock` | UI "lock now" button | Token | No (must already be unlocked) |
| Everything else (`/api/settings/*`, `/ws/voice`, `/api/tasks`, `/api/memory`, …) | After unlock | Token | **No — returns `423 Locked`** |

**Middleware order is load-bearing.** `auth.py:_PUBLIC_PATHS` (the existing token-auth bypass list) MUST be extended to include `POST /api/auth/bootstrap`, `POST /api/auth/unlock`, `POST /api/auth/lock`, and `GET /api/auth/state`. The vault-locked middleware (returns 423) runs *after* token auth; both must allow the auth endpoints through.

**Rate limit on `unlock` MUST fire before any call to `vault.unlock()`.** Implementation: an in-process counter checked at the top of the handler, returning `429 Too Many Requests` if exceeded. The Argon2id KDF allocates 256 MiB; four concurrent unlocks would exhaust the 1 GiB container memory cap. Rate limit: 1 attempt per 2 seconds, global (single-user model). No hard lockout (the encrypted blob is just bytes — a lockout would be theater).

**Bootstrap idempotency MUST be atomic.** `POST /api/auth/bootstrap` creates `data/kdf.salt` with `O_CREAT | O_EXCL` semantics — the file-creation atomicity is the mutex. If the file already exists, return `409 Conflict`. There must be no check-then-act window between `vault.is_initialized()` and salt creation.

## 6. Cryptography

| Parameter | Value | Why |
|---|---|---|
| KDF | Argon2id | Memory-hard, modern, the OWASP recommendation |
| Argon2 memory cost | 256 MiB | Strong against GPU/ASIC. Container has 1 GiB cap; ~256 MiB peak for the KDF is well within budget. |
| Argon2 time cost | 3 iterations | ~1 sec on the target M-series Macs; acceptable lock-screen latency. |
| Argon2 parallelism | 4 | Matches typical M-series core count. |
| Argon2 output length | 32 bytes | SQLCipher key. |
| Salt | 16 bytes random, stored at `data/kdf.salt` mode 0644 (public by design) | Generated once on bootstrap via `O_CREAT \| O_EXCL`; never rotated within a single passphrase generation. |
| Cipher | SQLCipher 4.x defaults (AES-256-CBC + HMAC-SHA512 + 256K KDF iterations per page) | Battle-tested. **Pinned via `PRAGMA cipher_compatibility = 4`** on every connection to prevent silent downgrade if the bundled library version changes. |
| Key material lifetime | Held as `bytearray`, zeroed via `ctypes.memset` on `vault.lock()` | `bytes` are immutable; `bytearray` allows deterministic zeroing. |

Key handling in memory:
- Master key held in a single module-level variable inside `vault.py`. Never written to disk, never logged, never returned over an API.
- On `vault.lock()`, the variable is overwritten with `bytes(32)` then `del`'d. SQLCipher connection objects closed in the same call.
- No `__repr__` on `VaultSession` exposes the key.

Dependency added: `pysqlcipher3` (Python binding for SQLCipher). This brings in libsqlcipher at the OS level — must be installed in the Docker image. Adds ~6 MiB to the image and one `apt-get install libsqlcipher-dev sqlcipher` line.

## 7. UI flow (lock-screen + first run)

Frontend's existing `main.ts` state machine gets two new states above the current voice-loop entry:

1. **Boot:** call `GET /api/auth/state`.
2. **First run** (`!initialized`): show a "Set passphrase" form. User picks a passphrase, confirms it. Submit to `POST /api/auth/bootstrap`. On success → unlocked → settings UI for entering API keys. (Existing `/api/settings/keys` endpoints handle the entry; `.env` import runs in the background on first unlock and the form pre-fills any pre-existing values.)
3. **Locked** (`initialized && locked`): show an unlock form (passphrase only). Submit to `POST /api/auth/unlock`. On 401, shake + show "wrong passphrase." On success → enter voice-loop UI.
4. **Unlocked:** unchanged from today, except the existing settings panel now writes to `secrets.db` via the same `/api/settings/keys` endpoint (no UI change needed in that panel).

The lock-screen is a separate Three.js orb state — the orb is dim and stationary until unlocked, then pulses. Reuses the existing visual vocabulary.

## 8. Migration (the one risky operation, made safe)

Runs **once** on the first successful `bootstrap` or `unlock` where legacy files are detected. Order:

1. **Detect:** `data/jarvis.db` exists as plain SQLite (the migration is idempotent; if `vault.session().memory_conn` already has tables, skip).
2. **Backup:** copy `data/jarvis.db` → `data/jarvis.db.pre-encrypt.bak`. Copy `.env` → `data/.env.pre-encrypt.bak`. Both with chmod 600.
3. **Import memory:** open plaintext `memory.db` read-only, iterate rows, insert into the encrypted `memory.db` via `vault.session().memory_conn`. Inside a single transaction.
4. **Import .env:** if `/app/.env.bootstrap` exists in the container (only present when the user opts into the bind mount documented in §9), parse it with the existing `_read_env` logic and write each whitelisted key (the same `allowed` set from `server.py:2627`) into `secrets.db`. If the file is absent, this step is a no-op — the user will enter keys via the UI settings panel after unlock.
5. **Verify:** count rows in encrypted `memory.db` per table; compare to plaintext counts. Compare `secrets.db` values to the parsed env values. On any mismatch → abort migration, leave the encrypted DBs in their pre-migration state, surface the error to the UI. The `.bak` files remain.
6. **Delete originals:** `os.remove("data/jarvis.db")` and `os.remove(".env")` (or the optional bind-mount path, see §9).
7. **Auto-delete backups on next successful unlock:** the `.bak` files (`data/jarvis.db.pre-encrypt.bak`, `data/.env.pre-encrypt.bak`) are kept for **exactly one** unlock cycle as a safety net. On the SECOND successful unlock after migration, they are deleted automatically. The UI also shows a "Clear migration backups now" button between the first and second unlock for users who want to purge immediately. This bounds plaintext key coexistence to a single restart window.

**`os.getenv` audit (must complete before migration ships).** Every call to `os.getenv("ANTHROPIC_API_KEY")`, `os.getenv("FISH_API_KEY")`, and `os.getenv("FISH_VOICE_ID")` in the codebase must be replaced with `vault.session().secrets.get(...)` or its equivalent helper. Known call sites at design time: `server.py:74`, `server.py:75-77` (FISH_API_KEY / FISH_API_URL / FISH_VOICE_ID), `server.py:2635`, `server.py:2647`. The plan must include a `grep -nE 'os\.getenv\("(ANTHROPIC|FISH)_'` sweep and a test asserting no such call survives outside `vault.py` and the legacy-import path.

If migration fails partway: the encrypted DBs may have a partial write — they're new in this run, so we drop and recreate them on next attempt. The user's data lives in the `.bak` files; they're never the only copy.

## 9. Docker compose changes

```yaml
services:
  backend:
    # ... existing config ...
    env_file: []                    # remove .env auto-load
    volumes:
      - ./data:/app/data:rw         # unchanged; now also holds secrets.db + kdf.salt
    # Optional bootstrap convenience: mount .env read-only so first-run import
    # can pick up keys without typing them in the UI. Documented as optional.
    # - ./.env:/app/.env.bootstrap:ro
```

The `env_file: []` clears compose's auto-injection of `.env` into the container's process env. The server itself never reads `os.environ["ANTHROPIC_API_KEY"]` anymore — it reads from `vault.session().secrets`.

For the `.env.bootstrap` import path: if the file is present on first boot, the vault import step reads it and copies the keys in. Documented in `docs/DOCKER.md` as an optional setup convenience.

## 10. Tests

Added under `tests/`:

- `tests/test_vault.py` — hermetic. Uses a tmp dir.
  - `bootstrap → unlock → settings round-trip`.
  - Wrong passphrase raises `VaultLockedError`, files untouched.
  - `lock()` zeroes in-memory key material (introspect via a test-only hook).
  - Argon2 parameters match §6 (constants test).
  - Migration: build a fixture plaintext `memory.db` + `.env`, run migration, assert encrypted contents match and originals are renamed to `.bak`.
  - Migration verification failure: corrupt the encrypted DB mid-flight, assert plaintext originals survive.
- `tests/test_server_locked_state.py` — hermetic FastAPI test client.
  - Every non-allowlisted `/api/*` returns `423` when locked.
  - `/api/health` and `/api/auth/*` reachable while locked.
  - `/api/auth/unlock` with wrong passphrase returns 401.
- `tests/test_goal_drift.py` — unchanged (already excluded from default collection).

`code-reviewer` persona to verify these are wired before merge.

## 11. Threat model deltas (changes to SECURITY.md)

Adds:
- Sensitive data at rest is encrypted with a user-derived key. Default deployment posture: encrypted SQLite via SQLCipher 4.x (`PRAGMA cipher_compatibility = 4`), master key derived by Argon2id (256 MiB / t=3 / p=4) from a user-supplied passphrase, never persisted.
- The passphrase is the single root of trust for at-rest data. **Loss = permanent data loss; no recovery path exists.** Operators MUST keep an out-of-band backup of the passphrase.
- The vault is locked across container restarts; the voice loop is unavailable until unlocked.
- `data/.local_token` no longer exists. The auth token is generated in-process on first request after unlock and stored in `secrets.db`. Tooling that read `data/.local_token` directly must now query `/api/auth/state` and use the unlock flow.
- `data/audit.jsonl` remains plaintext, append-only — this is a deliberate forensic preservation choice. An attacker with disk access can read it; an attacker who steals only the encrypted DBs cannot. Operators concerned about audit-log confidentiality should rely on FileVault.
- `data/jarvis.db.pre-encrypt.bak` and `data/.env.pre-encrypt.bak` are plaintext backups created during migration. They are auto-deleted on the second successful unlock after migration. The window of plaintext coexistence is bounded to one restart cycle by design.
- `data/kdf.salt` is public by design (mode 0644). It must not be rotated independently of a passphrase change.
- **Remote brute-force exposure when `--host 0.0.0.0` is used:** an attacker with LAN access can attempt the unlock at the rate-limited rate of 1 attempt / 2 seconds. The Argon2id cost makes the KDF itself slow; the passphrase strength is the load-bearing defense. Operators MUST use a strong passphrase (zxcvbn ≥ 3) if exposing on LAN; the frontend enforces this at bootstrap time as a UX guardrail.

Updates to data classification table:
- `ANTHROPIC_API_KEY`, `FISH_API_KEY`, `FISH_VOICE_ID`: at-rest → `data/secrets.db` (SQLCipher, Argon2id-derived key)
- `auth_token`: at-rest → `data/secrets.db` (was `data/.local_token` file)
- Memory database `data/jarvis.db`: at-rest → SQLCipher with same master key as secrets

Operator checklist additions:
- Set a strong passphrase on first run (zxcvbn ≥ 3 enforced client-side).
- Keep an out-of-band backup of the passphrase. There is no recovery.
- After first unlock, the second unlock clears the plaintext migration backups automatically.

Removes:
- "API keys live in `.env`" — replaced.
- "Memory.db is plaintext, FileVault is the at-rest defense" — replaced (FileVault stays as defense in depth).
- "`auth_token` file at `data/.local_token` mode 0600" — replaced.

Tripwire hook fires on edits to `SECURITY.md` per the existing personas system; security-advisor must review the diff.

## 12. Open questions — resolved by security-advisor review

1. **Argon2 calibration:** 256 MiB / t=3 / p=4 retained. Acceptable for the threat model. Optional upgrade to 512 MiB / t=4 deferred to a later PR if perf budget allows.
2. **Migration sequencing:** auto-delete on second successful unlock (per §8 step 7). Bounds plaintext window to one restart.
3. **`pysqlcipher3` bundled libsqlcipher:** accepted with explicit version pin in `requirements.txt` + `PRAGMA cipher_compatibility = 4` to lock the cipher version. CVE response = package bump.
4. **Unlock rate limit:** flat 1/2s, applied **before** KDF invocation. Loopback-bound by default keeps the threat model local. Progressive delay deferred (would be useful only when `--host 0.0.0.0` is set; documented in §11).
5. **Passphrase entropy:** zxcvbn ≥ 3 enforced client-side at bootstrap as a UX guardrail. Server does not validate (user is sovereign over their own data).

## 13. Out-of-scope follow-ups (separate backlog items)

- Passphrase change UI (re-encrypt both DBs with a new key).
- Backup/restore tooling for encrypted DBs.
- Auto-lock on idle (configurable timeout).
- Cloud-sync of encrypted DBs.
- Passphrase hint field (deferred from brainstorming).
