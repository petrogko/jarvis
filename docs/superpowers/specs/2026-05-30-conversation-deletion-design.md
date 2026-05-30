# Conversation Deletion + Auto-Expiry — Design

**Status:** design (for review)
**Date:** 2026-05-30
**Roadmap item:** Phase 1B / S2 in `docs/superpowers/roadmap/2026-05-30-aria-counsel-readiness.md`
**Persona routing:** `security-advisor` MUST review (touches vault-backed persistence, audit log, voice-action surface) → `code-reviewer` before commit → `test-runner` before any "ready to merge" claim.

---

## 1. Goals

1. **On-demand deletion of any single conversation** (the row in `conversations` + cascade across `messages`) via REST and via voice.
2. **Optional per-conversation TTL** ("this expires in an hour") so one-off sensitive sessions don't linger in the vault.
3. **Background sweeper** that hard-deletes expired conversations on a fixed interval, vault-permitting.
4. **Confirmation discipline** on voice deletion: a single misheard "burn it" must not destroy data.
5. **Audit trail** sufficient to answer "what got deleted, when, by which token" without ever logging conversation content.

## 2. Non-goals

- Soft delete / tombstones / undo. Counsel-grade means *gone*. See §7.
- Bulk "delete all" or "wipe vault" voice commands in this phase — deferred to security-advisor (§11 Q1, Q2).
- Frontend UI work; PR ships API + voice + sweeper. History-panel delete button is Phase 4.
- Cascading deletion into `memories` rows referencing a conversation — open question for advisor (§11 Q3).

## 3. Threat model

| Threat | Mitigation |
|---|---|
| Sensitive conversation persists indefinitely under the user's threat profile (device theft, household access). | DELETE endpoint + TTL + hard delete. |
| Voice misrecognition triggers deletion ("burn it" heard mid-sentence). | Two-turn confirm with stored intent + affirmative-regex gate (§5). |
| Sweeper runs while vault is locked → operates on stale handle or noisy errors. | Sweeper checks `vault.is_unlocked()` per pass; no-op otherwise. |
| Audit log leaks deleted content. | Log only `{ts, conversation_id, message_count_deleted, token_fp}`. No titles, no message text, no targets. Sanitizer reuse from `audit_log.record`. |
| Expired conversation deleted but `memories` row still cites it → dangling reference. | Documented as open question for advisor; default conservative: leave memory rows, store `source_conversation_id` as opaque. |

## 4. API surface

All three endpoints sit behind the existing vault-locked middleware (same posture as `GET /api/conversations`). All require `X-JARVIS-Token`.

### 4.1 `DELETE /api/conversations/{id}`

- 200: `{"deleted": <n>}` where `n = messages.rowcount` (0 is valid when an empty row is wiped; the conversation row itself was deleted iff `204`-equivalent counted).
- 404: unknown id.
- 423 (locked): vault locked (middleware-level; same as siblings).
- Implementation: single transaction via `conversations._get_conn()`. `DELETE FROM messages WHERE conversation_id = ?; DELETE FROM conversations WHERE id = ?;`. Emit one audit entry on success (§10).

### 4.2 `PATCH /api/conversations/{id}`

Accepts JSON body, partial. Recognized keys:

- `ttl_seconds: int` — sets `expires_at = now() + ttl_seconds`. `0` or negative → 400. `null` → clear expiry (`expires_at = NULL`).
- `title: str` — max 200 chars, sanitized via `untrusted_content.sanitize`.
- `seal: true` — shorthand for `ttl_seconds = 3600` AND title prefix `"[sealed] "`. Pure convenience for voice flow.

Returns 200 with the updated row summary `{id, title, expires_at}`. 404 if unknown. Unknown keys → 400.

### 4.3 Background sweeper

Asyncio task scheduled on FastAPI startup. Period: **300 s** (5 min). Per pass:

1. If `not vault.is_unlocked()` → no-op, log nothing, sleep.
2. Open conn via `_get_conn()`. `SELECT id FROM conversations WHERE expires_at IS NOT NULL AND expires_at <= ?`.
3. For each id: same delete transaction as §4.1. Accumulate `(count_conversations, total_messages)`.
4. If `count_conversations > 0`: one audit entry `{source: "sweeper", count_conversations, total_messages}`. **No ids.**
5. Sleep until next interval; cancellable on shutdown.

Drift-tolerant: missing a wake doesn't matter — expiry is a `<=` comparison.

## 5. Voice command surface

Two new actions added to `extract_action` in `server.py` and to the persona prompt's action catalogue.

### 5.1 `[ACTION:EXPIRE_CONVERSATION <target> <duration>]`

- `<target>`: `current` (the active conversation id from the WS session state) or a UUID.
- `<duration>`: `now` (delete immediately — alias for `DELETE_CONVERSATION`), `1h`, `30m`, `24h`, `7d`. Parser is the existing duration helper or a small one. Cap at 30 days.
- Backend translates to `PATCH ... {ttl_seconds: <parsed>}`. No confirmation required — setting a TTL is reversible (`null` clears it).

### 5.2 `[ACTION:DELETE_CONVERSATION <target>]`

Two-turn confirm protocol:

1. **Turn N:** Aria emits the action. Server detects it, does **not** delete. Instead:
   - Stores `pending_deletion = {conversation_id, expires_at: now+60s, requested_at}` on the WS session state.
   - Returns voice line: `"Confirm: burn this conversation? Say yes to wipe it, anything else cancels."`
2. **Turn N+1:** Server intercepts the next user utterance *before* LLM dispatch:
   - If `pending_deletion` exists and unexpired AND utterance matches `^(yes|yeah|yep|confirm|do it|burn it|wipe it)\b` (case-insensitive, after strip) → execute DELETE, clear pending, voice line: `"Gone."`
   - Else → clear pending, voice line: `"Cancelled."`, then dispatch the utterance to the LLM as normal.
   - 60 s timeout silently clears `pending_deletion` on the next interaction.

Affirmative regex lives next to the existing wake/cancel helpers, not in persona prompt. Persona is told only that confirmation is required; it MUST NOT generate the user's "yes" itself.

## 6. Schema additions

```sql
ALTER TABLE conversations ADD COLUMN expires_at REAL;  -- unix epoch seconds, NULL = never
CREATE INDEX IF NOT EXISTS idx_conversations_expires_at
  ON conversations(expires_at) WHERE expires_at IS NOT NULL;
```

Partial index keeps the sweeper SELECT O(expired-count). Migration is idempotent (`ADD COLUMN` guarded by a `PRAGMA table_info` check at module import, same pattern as PR #25).

`messages.conversation_id` already has a foreign-key declaration; we still do an explicit `DELETE FROM messages` first because SQLite enforces FKs only when `PRAGMA foreign_keys = ON` is set per connection (PR #25 sets it; the explicit DELETE is defense in depth and gives us the rowcount).

## 7. Hard vs soft delete — decision

**Hard delete.** No tombstone row, no recoverable shadow table. The trade-off is explicit: there is no undo. This matches the threat model (the user is deleting because they want it *gone*; a soft-delete is a footgun in a counsel context). The 60 s voice-confirm window is the only safety net and is sufficient for the most common misfire (misrecognition).

For accidental REST deletes: the API is gated by the auth token; we treat the caller as authoritative.

## 8. Lock-aware semantics

- Endpoints inherit the vault-locked middleware: a locked vault → 423, period. No special case.
- Sweeper polls `vault.is_unlocked()` *before opening a connection*. Skipping a pass costs at most 5 minutes of TTL drift, which is acceptable.
- Sweeper does NOT auto-unlock or hold a reference to the vault key. It uses the same connection factory as the request path.

## 9. Frontend touch (deferred)

History panel (Phase 4) will consume:

- `DELETE /api/conversations/{id}` from a per-row trash button (browser-side confirm).
- `PATCH /api/conversations/{id}` with `{ttl_seconds}` from a "self-destruct" dropdown (1h / 1d / 7d / never).

Backend ships now; UI binds later.

## 10. Audit log

One entry per delete event, written via `audit_log.record` with a new `action="delete_conversation"`:

```json
{"ts":"…","source":"api-delete|sweeper|voice-confirm",
 "action":"delete_conversation",
 "target_summary":"<conversation_id>",
 "user_text_summary":"<message_count_deleted>",
 "success":true,
 "reason":"<token_fp first 8 chars, or 'sweeper'>"}
```

**Invariant:** `target_summary` is the UUID (already non-secret); `user_text_summary` is a stringified integer; `reason` is the SHA256-truncated token fingerprint or the literal `"sweeper"`. **No titles, no message bodies, no expires_at.** Sweeper writes one aggregated entry per pass with `target_summary="<n>"` (count of conversations) and `user_text_summary="<total_messages>"`.

## 11. Tests (names)

In `tests/test_conversation_deletion.py`:

- `test_delete_happy_returns_count_and_wipes_messages`
- `test_delete_unknown_id_returns_404`
- `test_delete_when_vault_locked_returns_423`
- `test_patch_ttl_sets_expires_at_and_sweeper_deletes`
- `test_patch_ttl_null_clears_expiry`
- `test_patch_rejects_unknown_keys_and_negative_ttl`
- `test_sweeper_noop_when_vault_locked`
- `test_sweeper_logs_count_only_no_ids`
- `test_voice_confirm_two_turn_affirmative_deletes`
- `test_voice_confirm_two_turn_non_affirmative_cancels_and_passes_through`
- `test_voice_confirm_timeout_60s_clears_pending`
- `test_audit_entry_omits_titles_and_message_bodies`
- `test_expire_action_parses_durations_and_caps_at_30d`

## 12. Open questions for security-advisor

1. **"Delete all" voice command** — useful in duress? Or footgun? If yes, what confirmation (passphrase re-entry)?
2. **Vault-wipe voice command** — does Aria need a "nuke everything" verb, or do we rely on the user destroying the vault file out-of-band?
3. **Cascade into `memories`** — when a conversation is deleted (manual or via sweeper), should we also delete `memories` rows whose `source_conversation_id` matches? Default proposal: leave them, since memories are summarized abstractions, not raw content. Advisor to confirm.
4. **Token-fingerprint format in audit** — is the first 8 chars of SHA256 sufficient (collision-tolerant for forensics, non-reversible)? Or should we use a per-restart salt to defeat rainbow-table linking across vault sessions?
5. **Sweeper interval drift** — 5 minutes acceptable, or should we tighten to 60 s for short TTLs (e.g. 5-minute "burn after read")?
