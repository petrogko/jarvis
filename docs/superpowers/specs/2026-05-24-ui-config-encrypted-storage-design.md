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
5. **Safe migration.** Existing `data/memory.db` (plain SQLite today) and `.env` are imported on first successful unlock; originals are kept until import is verified.

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
| `data/memory.db` | Conversation memory, tasks, audit (replaces today's plain SQLite memory) | Existing schema from `memory.py` + tasks + audit, ported under SQLCipher. |

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
- `auth.py` — auth token now lives in `secrets.db` table not the `auth_token` file. The startup flow:
  1. Container starts, no vault unlocked.
  2. All `/api/*` return `423` except auth + health.
  3. First request after unlock: `auth.py` reads the token from `vault.session().secrets`. If missing, generates one.

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

Rate limit on `unlock`: 1 attempt per 2 seconds, no hard lockout (would be theater — see §3 reasoning).

## 6. Cryptography

| Parameter | Value | Why |
|---|---|---|
| KDF | Argon2id | Memory-hard, modern, the OWASP recommendation |
| Argon2 memory cost | 256 MiB | Strong against GPU/ASIC. Container has 1 GiB cap; ~256 MiB peak for the KDF is well within budget. |
| Argon2 time cost | 3 iterations | ~1 sec on the target M-series Macs; acceptable lock-screen latency. |
| Argon2 parallelism | 4 | Matches typical M-series core count. |
| Argon2 output length | 32 bytes | SQLCipher key. |
| Salt | 16 bytes random, stored at `data/kdf.salt` | Generated once on bootstrap; never rotated within a single passphrase generation. |
| Cipher | SQLCipher's default (AES-256 CBC + HMAC-SHA512) | Battle-tested, ships with `pysqlcipher3`. |

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

1. **Detect:** `data/memory.db` exists as plain SQLite (the migration is idempotent; if `vault.session().memory_conn` already has tables, skip).
2. **Backup:** copy `data/memory.db` → `data/memory.db.pre-encrypt.bak`. Copy `.env` → `data/.env.pre-encrypt.bak`. Both with chmod 600.
3. **Import memory:** open plaintext `memory.db` read-only, iterate rows, insert into the encrypted `memory.db` via `vault.session().memory_conn`. Inside a single transaction.
4. **Import .env:** if `/app/.env.bootstrap` exists in the container (only present when the user opts into the bind mount documented in §9), parse it with the existing `_read_env` logic and write each whitelisted key (the same `allowed` set from `server.py:2627`) into `secrets.db`. If the file is absent, this step is a no-op — the user will enter keys via the UI settings panel after unlock.
5. **Verify:** count rows in encrypted `memory.db` per table; compare to plaintext counts. Compare `secrets.db` values to the parsed env values. On any mismatch → abort migration, leave the encrypted DBs in their pre-migration state, surface the error to the UI. The `.bak` files remain.
6. **Delete originals:** `os.remove("data/memory.db")` and `os.remove(".env")`. The `.bak` files stay until the user clicks "Clear migration backups" in the UI (a settings button that appears only when backups exist).

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
- Sensitive data at rest is encrypted with a user-derived key. Default deployment posture: encrypted SQLite via SQLCipher, master key derived by Argon2id from a user-supplied passphrase, never persisted.
- The passphrase is the single root of trust for at-rest data. Loss = data loss; no backdoor.
- The vault is locked across container restarts; the voice loop is unavailable until unlocked.

Removes:
- "API keys live in `.env`" — replaced.
- "Memory.db is plaintext, FileVault is the at-rest defense" — replaced (FileVault stays as defense in depth).

Tripwire hook fires on edits to `SECURITY.md` per the existing personas system; security-advisor must review the diff.

## 12. Open questions (for security-advisor)

1. Is Argon2id memory=256 MiB / time=3 / parallelism=4 the right calibration for this threat model and the 1 GiB container budget? Should we bump memory cost?
2. Is the migration's "delete originals after verify" sequencing correct, or should we leave originals indefinitely and add a manual "purge" button? Risk vs. ergonomics.
3. Is `pysqlcipher3` the right binding (it bundles libsqlcipher), or should we link against system libsqlcipher (smaller image, more moving parts in CI)?
4. Rate-limiting `unlock` — is 1/2s sufficient, or should we add a progressive delay (e.g. doubling) to defeat brute-force without a hard lockout?
5. Should the bootstrap flow enforce a minimum passphrase entropy (zxcvbn ≥3)? Or trust the user?

## 13. Out-of-scope follow-ups (separate backlog items)

- Passphrase change UI (re-encrypt both DBs with a new key).
- Backup/restore tooling for encrypted DBs.
- Auto-lock on idle (configurable timeout).
- Cloud-sync of encrypted DBs.
- Passphrase hint field (deferred from brainstorming).
