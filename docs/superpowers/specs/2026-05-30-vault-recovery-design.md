# Vault Paper-Recovery Code — Design

**Status:** design (for review)
**Date:** 2026-05-30
**Roadmap item:** Phase 1E in `docs/superpowers/roadmap/2026-05-30-aria-counsel-readiness.md`
**Persona routing:** `security-advisor` MUST review (new credential surface, second route to K_data, KDF-adjacent) → `code-reviewer` before commit → `test-runner` before any "ready to merge" claim.

---

## 1. Goals

1. **One-time printed recovery code** generated at vault bootstrap, displayed to the user once, never persisted in plaintext.
2. **Unlock-with-recovery** flow that recovers vault access after passphrase loss, *and* forces the user to set a new passphrase as part of the same operation.
3. **Rotation** of the recovery code from settings, gated on the current passphrase.
4. **Upgrade migration**: existing single-wrap vaults get a recovery wrap on first unlock after upgrade; the code is shown once.
5. **Audit trail** sufficient to notice recovery use (anomalous) without ever logging the code itself.

## 2. Non-goals

- Cloud escrow / Shamir splits / social recovery. Out of scope; the user is the custodian.
- Recovery without setting a new passphrase. A successful recovery flow MUST replace the passphrase wrap — otherwise the user remains one passphrase-loss away from the same problem.
- Duress / coerced recovery. Phase 3, S6.
- Recovery code change on every unlock. The pair (passphrase, recovery) is stable until rotated.

## 3. Threat model

Counsel-grade means losing access is catastrophic, but the recovery code IS a second route to the vault. It must be at least as hard to attack as the passphrase. Storage of the code is the user's responsibility — print, lock in a safe / safe-deposit / encrypted iCloud Notes. JARVIS never sees it again after the bootstrap screen.

| Threat | Mitigation |
|---|---|
| Lost passphrase, code in safe. | `unlock-with-recovery` reissues passphrase. |
| Lost passphrase AND lost code. | Unrecoverable. Documented. Intended. |
| Code stolen, passphrase safe. | Attacker has full vault access. Intended — code is a credential. Mitigation is offline storage. |
| Online brute-force of recovery code. | Same `_LAST_UNLOCK_ATTEMPT` rate-limit as passphrase. 256-bit entropy makes brute force infeasible regardless. |
| Code re-displayed by triggering re-bootstrap or by reloading the bootstrap page. | Bootstrap is `O_EXCL` (already). Display screen is shown once per process; reload routes to the locked screen, not bootstrap. Code is unrecoverable from disk after acknowledgement (only its wrap is stored). |
| Clipboard exfiltration by another app. | **No copy-to-clipboard button.** Print and hand-transcribe only. |
| Shoulder-surfing during display. | Display screen carries a "Hide" toggle; words are revealed on hover. Documented in §4. |
| Coerced recovery. | Out of scope (Phase 3 S6 duress mode). |

## 4. Crypto design

Current vault: `K_data` (the SQLCipher master key) is `Argon2id(passphrase, salt)` directly. There is no separable data key; the KDF output IS the key.

Proposed change: introduce an explicit `K_data` decoupled from the KDF, wrap it under two independent paths.

### 4.1 Keys

- `K_data` — 32 random bytes generated at bootstrap. Becomes the SQLCipher key for both `secrets.db` and `jarvis.db`.
- `K_pass = Argon2id(passphrase, salt)` — unchanged parameters (256 MiB / t=3 / p=4 / 32 B). Used only to wrap `K_data`.
- `K_recovery` — 32 random bytes. Displayed to the user as 24 BIP-39 words. Used only to wrap `K_data`. **No KDF on the recovery path** — the code IS the high-entropy key material; running Argon2id on it would only add latency without security.

### 4.2 Wraps

Both wraps use authenticated encryption: ChaCha20-Poly1305 (stdlib via `cryptography`), 12-byte nonce per wrap, 16-byte tag. Stored as `nonce || ciphertext || tag`.

- `wrap_passphrase = AEAD(K_pass, nonce_p, K_data, aad="wrap_passphrase")`
- `wrap_recovery   = AEAD(K_recovery, nonce_r, K_data, aad="wrap_recovery")`

AAD binds each ciphertext to its slot so a swapped row can't impersonate the other.

### 4.3 Schema additions

New table in `secrets.db`:

```sql
CREATE TABLE IF NOT EXISTS recovery_wrap (
  slot TEXT PRIMARY KEY,        -- 'passphrase' | 'recovery'
  blob BLOB NOT NULL,            -- nonce || ct || tag
  updated_at TEXT NOT NULL
);
```

The KDF salt (`data/kdf.salt`) is unchanged.

### 4.4 Flows

- **`bootstrap(passphrase)`**: generate `K_data`, `K_recovery`; derive `K_pass`; write both wraps; create encrypted DBs keyed with `K_data`. Return `K_recovery` encoded as 24 BIP-39 words to the caller (one-shot — never written to disk).
- **`unlock(passphrase)`**: derive `K_pass`, AEAD-open `wrap_passphrase` → `K_data`, open DBs.
- **`unlock_with_recovery(code, new_passphrase)`**: decode BIP-39 → `K_recovery`, AEAD-open `wrap_recovery` → `K_data`, open DBs, then immediately derive `K_pass' = Argon2id(new_passphrase, salt)`, regenerate a fresh `wrap_passphrase'` over the same `K_data`, replace the row. Also rotate `K_recovery` (see §10 Q2) and return the new code.
- **`rotate_recovery(current_passphrase)`**: unlock-as-usual gate; mint new `K_recovery`, rewrap `K_data`, replace `wrap_recovery`. Return the new code.

`K_data` lives in the same zeroed `bytearray` discipline as the current key (see `_zero_bytearray`). `K_recovery` is zeroed immediately after use.

### 4.5 Format choice — BIP-39

24 words = 256 bits entropy + 8-bit checksum. Rationale:

- Wordlist is 2048 short, distinct English words; well-tested transcription error properties.
- Built-in checksum catches single-word and most multi-word transcription errors before we hit the AEAD (which would otherwise return a generic "wrong code").
- Easier to read aloud and to hand-transcribe than 52-char base32 or 64-char hex.
- Trade-off: importing a wordlist (~13 KB). Acceptable.

Rejected: base32 (`XXXX-XXXX-...`) — denser but no checksum, harder to read aloud (case-sensitive ambiguity). Hex — worst on both axes.

## 5. API surface

All endpoints sit alongside the existing `/api/auth/*`. Rate-limit shared with `unlock` via `_LAST_UNLOCK_ATTEMPT`.

### 5.1 `POST /api/auth/bootstrap`

Body unchanged: `{passphrase}`. Response now includes the recovery code **once**:

```json
{ "ok": true, "recovery_code": "abandon ability ... zoo" }
```

Subsequent calls fail with 409 (`VaultExistsError`) — the code is unrecoverable.

### 5.2 `POST /api/auth/unlock-with-recovery`

Body: `{recovery_code, new_passphrase}`.

- 200: `{ok: true, token, recovery_code}` — the **new** recovery code (rotated; see §10 Q2). Vault is unlocked; passphrase replaced.
- 401: invalid code (AEAD open failed or BIP-39 checksum failed).
- 429: rate-limit.
- 400: `new_passphrase` < 8 chars.

### 5.3 `POST /api/auth/rotate-recovery`

Body: `{current_passphrase}`. Requires vault unlocked AND passphrase re-supplied (defence in depth — token alone is not enough to mint a new recovery credential).

- 200: `{ok: true, recovery_code}`.
- 401: wrong passphrase.
- 423: vault locked.

## 6. UX — bootstrap display

After successful bootstrap (`renderFirstRun` in `frontend/src/lock-screen.ts`), a new state `renderRecoveryDisplay` is pushed before the unlocked transition:

```
Your recovery code
24 words, in order. Write them down and store them offline —
a safe, a safe-deposit box, or encrypted iCloud Notes.

  1. abandon      9. drift      17. nest
  2. ability     10. echo       18. ocean
  ...           ...            ...
  [ Reveal ]  [ Print ]
  [ ] I have written this down and stored it offline.
  [ Continue ]                                  ← disabled until checked
```

UX rules (counsel-grade):

- **No copy button.** Clipboard is shared across applications.
- **Reveal toggle**: words rendered as `•••••` until hovered/clicked; tap-and-hold on mobile. Prevents drive-by capture by anyone walking past.
- **Print button**: `window.print()` with a print-only stylesheet that includes the 24 words, the date, and a "Store offline. JARVIS cannot recover this." footer.
- **Continue is disabled** until the checkbox is ticked.
- **Reloading the page does NOT show the screen again.** After `Continue`, the frontend never sees the code; the server has only `wrap_recovery`. If the user closes the tab before clicking Continue, the wrap still exists and the unlock-with-recovery route still works — but the user no longer has the code and must rotate via §5.3 from the unlocked state. This is acceptable.

The lock-screen header copy `"There is no recovery."` is updated to `"Without your passphrase or recovery code, your vault is unrecoverable."`.

## 7. Migration (upgrade)

Existing vaults have `K_data == K_pass` and no `recovery_wrap` table. On first `unlock` after upgrade:

1. Detect: `recovery_wrap` table absent OR no row with `slot = 'recovery'`.
2. Generate `K_data_new` = 32 random bytes.
3. **Rekey** SQLCipher: `PRAGMA rekey = "x'<hex(K_data_new)>'"` on both DBs. Atomic per pysqlcipher3.
4. Wrap `K_data_new` under both `K_pass` (current) and a fresh `K_recovery`. Insert both rows.
5. Return a one-shot `migration_recovery_code` to the unlock response; frontend routes through `renderRecoveryDisplay` before continuing to the main UI.
6. If the user closes the tab without acknowledging, the next unlock takes the normal path (rows exist), but the user has no code. Settings must surface a "No recovery code on file — rotate to generate one" banner. (Open question §10 Q3 — should we block unlock until acknowledged?)

Failure during rekey is the only dangerous step. Mitigation: hot backup via `VACUUM INTO 'jarvis.db.preupgrade.bak'` (encrypted with the **old** `K_pass`) before rekey; delete on success.

## 8. Audit log

Three new verbs (sanitizer reuse from `audit_log.record`; no content):

- `vault_bootstrap` — `{ts, token_fp: null}`. Marks initial generation.
- `vault_recovery_used` — `{ts, token_fp, anomalous: true}`. The `anomalous` flag is the operator's tripwire.
- `vault_recovery_rotated` — `{ts, token_fp}`. Distinguishes user-initiated rotation from a recovery-driven rotation (the latter logs both `recovery_used` and `recovery_rotated`).

Never log the code, its hash, or any prefix. The audit log is plaintext; a hash would still narrow brute-force.

## 9. Tests

Named tests for the test-runner to expect:

1. `test_bootstrap_generates_both_wraps` — `secrets.db` after bootstrap has `recovery_wrap` rows for both slots.
2. `test_unlock_with_passphrase_after_bootstrap` — round-trip works; existing path unchanged.
3. `test_unlock_with_recovery_round_trip` — bootstrap, lock, unlock-with-recovery + new passphrase, then unlock with new passphrase succeeds.
4. `test_unlock_with_recovery_rejects_invalid_bip39_checksum` — wrong word substituted; 401 before AEAD is touched.
5. `test_unlock_with_recovery_rejects_aead_failure` — checksum-valid but wrong code; 401.
6. `test_unlock_with_recovery_rate_limited` — 5 wrong attempts in window → 429, shared with passphrase counter.
7. `test_rotate_recovery_requires_passphrase` — wrong `current_passphrase` → 401; correct → new code, old code stops working.
8. `test_rotate_recovery_invalidates_old_code` — explicit AEAD-open with old `K_recovery` against new wrap fails.
9. `test_recovery_code_format_24_bip39_words` — output is 24 space-separated words from the wordlist, valid checksum.
10. `test_bootstrap_response_is_one_shot` — second bootstrap → 409, no code in response.
11. `test_migration_generates_recovery_wrap_and_returns_code` — pre-1E vault on disk → unlock returns `migration_recovery_code` and persists `recovery_wrap`.
12. `test_audit_log_recovery_used_has_anomalous_flag_and_no_content` — verb present, code/hash absent.

## 10. Open questions for security-advisor

1. **Forward secrecy on rotation.** Should `rotate-recovery` also rotate `K_data` (full SQLCipher rekey)? Pros: a leaked old recovery code, *combined* with a database snapshot from before rotation, would otherwise still work against that snapshot. Cons: rekey on the memory DB is expensive and risks a half-rekeyed state. Recommendation: not in 1E; document as a known limitation; revisit if we add backup-export.
2. **Pair rotation.** Should passphrase rotation (a future Phase 1F feature) also rotate `K_recovery`? My current lean: **yes**, treat them as a pair — otherwise a stale recovery code can resurrect the old passphrase-protection state. `unlock-with-recovery` already does this; an explicit `change-passphrase` endpoint should too. Confirm.
3. **Migration enforcement.** Should the unlock-after-upgrade flow *block* completion until the user ticks the "I stored it" checkbox, or proceed and nag from settings? Blocking is safer but means an SSH-only user gets locked out of the UI until they sit down with a printer. Lean: nag, don't block.
4. **Out-of-band confirmation.** Worth requiring a second factor (e.g. re-typing the new passphrase 30 s later, or a TOTP if Phase 1G lands first) on `unlock-with-recovery`? Lean: no for 1E — the recovery code itself is the second factor by design.
5. **Memory zeroing of decoded BIP-39 buffer.** The frontend holds the 24 words in a `string` (immutable, GC'd at unknown time). Acceptable, or do we need a `Uint8Array`-backed input widget? Lean: acceptable; the words are public knowledge of the wordlist plus order, and the AEAD wrap is what gives access. The risk is screen-capture, addressed by §6 Reveal toggle.

---

**Files touched by the implementation PR:**

- `vault.py` — new `K_data` indirection, `recovery_wrap` table, `unlock_with_recovery`, `rotate_recovery`, migration helper.
- `server.py` — three endpoint handlers, rate-limit reuse.
- `frontend/src/lock-screen.ts` — `renderRecoveryDisplay`, copy-button removal, reveal toggle, print stylesheet.
- `audit_log.py` — three new verbs registered.
- `tests/test_vault_recovery.py` — new file, the 12 tests above.
