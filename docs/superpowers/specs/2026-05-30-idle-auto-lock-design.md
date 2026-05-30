# Idle Auto-Lock — Design

**Status:** design (for review)
**Date:** 2026-05-30
**Roadmap item:** Phase 1C in `docs/superpowers/roadmap/2026-05-30-aria-counsel-readiness.md`
**Persona routing:** `security-advisor` MUST review (touches `vault.lock`, the locked middleware, and the WS auth posture) → `code-reviewer` before commit → `test-runner` before any "ready to merge" claim.

---

## 1. Goals

1. Automatically relock the vault after a configurable period of inactivity, without restarting the container.
2. Cover the "Mac unattended with vault unlocked" scenario: a browser tab left open exposes the live transcript panel and a hot WS that can continue Aria's session.
3. Disconnect idle WS clients on lock so they cannot smuggle past the locked-vault middleware via an already-open socket.
4. Stay opt-out-able (advanced users) but keep the secure default on.

## 2. Non-goals

- OS-level lock / screensaver integration. We do not call `caffeinate` introspection or listen to macOS `IOPMSleepNotification` in this phase (open question §10).
- Per-conversation idle policy. One global threshold for now.
- Re-authentication challenges short of a full lock (e.g. "tap to confirm"). Counsel-grade means a real passphrase round-trip.
- Wiping in-memory caches (`anthropic_client`, `cached_projects`). Tracked as open question §10.

## 3. Threat model

| Threat | Mitigation |
|---|---|
| User leaves laptop unlocked; passer-by reads transcript panel / continues session. | Auto-lock after `IDLE_LOCK_S` (default 900 s). Frontend forced back to lock-screen. |
| Stale WS connection held by an attacker keeps the session "live" indefinitely (no HTTP requests = middleware never triggers). | Connected-but-silent WS counts as idle after `IDLE_LOCK_S * 2`; we force-close with code 4423. |
| Mac sleeps for hours, wakes; old WS resumes mid-utterance under a different physical user. | Same WS-close path; lock-screen on reconnect. |
| In-flight LLM call completes after lock; its persistence write reveals data on a locked vault. | `record_message` opens `vault.session()`; returns None → no-op. Acceptable loss of one final assistant turn (§7). |
| Auto-lock loop itself reads conversation content for "activity." | Loop only reads `_last_activity_ts` (a float) and a WS count. No user content ever touched. |

## 4. Trigger conditions

The lock fires when **either** is true:

- **(A) Quiet idle.** `_ws_clients_count() == 0` AND `now - _last_activity_ts > IDLE_LOCK_S`.
- **(B) Wandered WS.** `_ws_clients_count() > 0` AND `now - _last_activity_ts > IDLE_LOCK_S * 2`.

"Activity" is defined as the most recent of:

- Successful `vault.unlock` (initializes the timer to `now`).
- A message received on `/ws/voice` (text or binary frame; the handler updates the timer in its receive loop).
- A protected HTTP request that **passed** the locked middleware (i.e. vault was unlocked at request time — middleware is the chokepoint).

Activity is NOT counted for:

- Public paths (`/api/health`, `/api/auth/*`).
- Failed token auth (rejected before the locked middleware sees it).
- Outbound LLM responses on the server side (per spec, only **received** user signal counts — server emitting tokens to itself is not "user present").

## 5. Configuration

Two vault keys (both added to the secrets allowlist; both readable via `_vault_get`):

| Key | Type | Default | Effect |
|---|---|---|---|
| `IDLE_LOCK_S` | int (seconds) | `900` | Quiet-idle threshold. Wandered-WS threshold derived as `2 * IDLE_LOCK_S`. Range [60, 86400]; values outside clamp with a warn log. |
| `IDLE_LOCK_DISABLED` | bool (`"1"`/`"0"`) | `"0"` | If `"1"`, the loop is started but no-ops every tick. Documented as "vault remains unlocked until process restart — counsel-mode users SHOULD NOT set this." Visible in audit log at startup. |

Both keys live in the secrets DB and are mutable via the same UI-config surface as other settings.

## 6. Implementation

### 6.1 Activity timer

New module globals near the vault helpers in `server.py`:

```python
_last_activity_ts: float = time.time()
def _touch_activity() -> None:
    global _last_activity_ts
    _last_activity_ts = time.time()
```

Wired in at exactly three sites:

1. **`vault.unlock` success path** — `api_auth_unlock` calls `_touch_activity()` immediately after the session is established.
2. **`vault_locked_middleware`** — after `_vault_mod.session() is not None` confirms unlock, call `_touch_activity()` BEFORE `await call_next(request)`. Single chokepoint covers every protected HTTP path.
3. **`/ws/voice` receive loop** — every `await ws.receive_*()` that returns a frame calls `_touch_activity()` before dispatch.

### 6.2 Background task

In `lifespan()`, after the existing init block:

```python
idle_task = asyncio.create_task(_idle_lock_loop(), name="idle-lock")
try:
    yield
finally:
    idle_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await idle_task
```

Loop body (60 s tick):

```python
async def _idle_lock_loop():
    while True:
        await asyncio.sleep(60)
        if _vault_mod.session() is None: continue
        if _vault_get("IDLE_LOCK_DISABLED", "0") == "1": continue
        threshold = _read_idle_lock_s()
        n_ws = len(task_manager._websockets)  # via accessor
        idle_for = time.time() - _last_activity_ts
        triggered = (n_ws == 0 and idle_for > threshold) or \
                    (n_ws > 0 and idle_for > threshold * 2)
        if triggered:
            await _do_auto_lock(reason=("idle" if n_ws == 0 else "wandered-ws"),
                                 connection_count=n_ws)
```

### 6.3 Lock sequence (`_do_auto_lock`)

1. Snapshot WS list from `task_manager` (copy under a small lock to avoid mutation during iteration).
2. For each WS: best-effort `await ws.send_json({"type":"vault_locked","reason":...})`, then `await ws.close(code=4423)`. Swallow per-socket errors.
3. `vault.lock()` (already idempotent; closes both SQLCipher conns and zeros the key).
4. Set `anthropic_client = None`? — *deferred* (see open question §10.3).
5. `audit_log.record(...)` — see §8.
6. `_touch_activity()` to reset the timer (prevents an immediate retrigger if unlock is fast).

`vault.lock()` is the atomic boundary. Anything that began with a valid `vault.session()` before step 3 keeps its connection until it returns; SQLCipher closes are best-effort and tolerate "already closed" (§7).

## 7. Race conditions

- **In-flight LLM call** finishes after `vault.lock()`. Its `record_message` calls `vault.session()`, sees `None`, no-ops. The user's prompt was already delivered to Anthropic; the final assistant response is dropped on the floor and not persisted. Acceptable.
- **HTTP request already past the middleware** when lock fires: completes against connection handles that `vault.lock()` just closed. SQLCipher will raise; handler returns 500 to the client, which is already being kicked to lock-screen anyway.
- **Unlock racing the loop tick**: the loop reads `_vault_mod.session()` first; if unlock happens between the read and the lock call, we still lock. Mitigated by `_touch_activity()` on unlock — the loop's `idle_for` check will be false on the same pass. The `> threshold` (strict) comparison protects against zero-elapsed.
- **`vault.lock()` is single-threaded** (it sets `_session = None` first; per `vault.py` docstring callers must serialize at the app layer). The idle loop is the only auto-caller, so no contention with manual `/api/auth/lock` beyond what already exists.

## 8. Audit log

One entry per auto-lock, via `audit_log.record`:

```json
{"ts":"…","source":"idle-lock",
 "action":"auto_lock",
 "target_summary":"<idle|wandered-ws>",
 "user_text_summary":"<connection_count_at_lock>",
 "success":true,
 "reason":"<idle_for_seconds rounded to int>"}
```

**Invariant:** never any user content, conversation id, or token. `target_summary` and `reason` are bounded enums/integers. One extra entry at startup recording `IDLE_LOCK_S` and `IDLE_LOCK_DISABLED` so a forensic reader can see the configured posture.

## 9. Frontend reaction

`/ws/voice` client (`frontend/src/voice.ts` or wherever the socket is owned):

- On `message` with `{"type":"vault_locked"}`: stop audio, tear down voice state, call `awaitUnlock()` (existing flow in `frontend/src/main.ts` / `lock-screen.ts`).
- On socket `close` with `event.code === 4423` (or any abnormal close while the app believes the vault is unlocked): same path — `awaitUnlock()` re-prompts for passphrase.
- After unlock, `main.ts` already reinitializes the WS; no new code path needed beyond reading the close code.

No new UI: the existing lock-screen is the destination.

## 10. Open questions for security-advisor

1. **Counsel-mode threshold.** When a conversation is sealed (per Phase 1B spec §5.2), should the idle threshold automatically drop (e.g. to 120 s) for the duration of that conversation? Proposal: yes, but make it a separate vault key `IDLE_LOCK_S_SEALED`.
2. **System notification.** Should auto-lock fire a `osascript display notification "Aria locked"` so the user knows on return? Proposal: yes — pure UX, no content leak. Confirm AppleScript surface is acceptable.
3. **Cache wipe on lock.** Currently `anthropic_client` stays bound after lock (the key inside the SDK is just an API key, not vault-derived — but it is a vault-stored secret). Should `_do_auto_lock` also null `anthropic_client`, `cached_projects`, and any persona/system-prompt caches? Cost: a 1-2 s rebuild on next unlock. Proposal: yes for `anthropic_client` (it holds an API key in process memory); no for `cached_projects` (non-secret).
4. **macOS sleep hook.** Should we also subscribe to a sleep notification and lock immediately on sleep, rather than wait up to 60 s for the next tick? Proposal: out of scope for 1C; revisit in 1D.

## 11. Tests (names)

In `tests/test_idle_auto_lock.py` (uses `isolated_vault` fixture + `monkeypatch` on `time.time` and `asyncio.sleep`):

- `test_lock_fires_after_idle_lock_s_with_no_ws`
- `test_activity_resets_the_timer`
- `test_connected_but_idle_ws_triggers_lock_at_2x_threshold`
- `test_active_ws_messages_keep_session_alive`
- `test_opt_out_flag_disables_the_loop`
- `test_audit_log_entry_shape_idle_reason`
- `test_audit_log_entry_shape_wandered_ws_reason`
- `test_ws_clients_receive_vault_locked_message_before_close_4423`
- `test_protected_http_request_updates_activity_via_middleware`
- `test_loop_noop_when_vault_already_locked`
- `test_lock_is_idempotent_under_concurrent_tick_and_manual_lock`
