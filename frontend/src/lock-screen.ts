/**
 * Lock-screen state machine for JARVIS vault unlock.
 *
 * States:
 *   - boot: fetching /api/auth/state
 *   - first-run: user picks a passphrase
 *   - locked: user enters passphrase
 *   - unlocked: hand off to voice UI (resolves the start promise)
 *
 * Renders into an element with id="lock-screen". Removes itself from
 * the DOM on successful unlock.
 */

import { setToken, getToken } from "./auth-token";

type AuthState = { initialized: boolean; locked: boolean };
type UnlockResponse = { ok: boolean; token?: string };

async function fetchState(): Promise<AuthState> {
  const r = await fetch("/api/auth/state");
  return r.json();
}

async function postJson(path: string, body: object): Promise<Response> {
  return fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function renderFirstRun(container: HTMLElement): Promise<void> {
  return new Promise((resolve) => {
    container.innerHTML = `
      <div class="lock-card">
        <h1>Welcome to JARVIS</h1>
        <p>Set a passphrase. JARVIS will require it on every restart.
           <strong>There is no recovery.</strong></p>
        <input type="password" id="lock-pp1" placeholder="passphrase" autofocus />
        <input type="password" id="lock-pp2" placeholder="confirm passphrase" />
        <div class="lock-err" id="lock-err"></div>
        <button id="lock-submit">Set passphrase</button>
      </div>`;
    const btn = container.querySelector("#lock-submit") as HTMLButtonElement;
    btn.onclick = async () => {
      const pp1 = (container.querySelector("#lock-pp1") as HTMLInputElement).value;
      const pp2 = (container.querySelector("#lock-pp2") as HTMLInputElement).value;
      const err = container.querySelector("#lock-err") as HTMLElement;
      if (pp1.length < 8) { err.textContent = "Passphrase too short (min 8)"; return; }
      if (pp1 !== pp2) { err.textContent = "Passphrases do not match"; return; }
      err.textContent = "Bootstrapping…";
      const r = await postJson("/api/auth/bootstrap", { passphrase: pp1 });
      if (!r.ok) { err.textContent = `Error ${r.status}`; return; }
      const r2 = await postJson("/api/auth/unlock", { passphrase: pp1 });
      if (!r2.ok) { err.textContent = `Unlock failed: ${r2.status}`; return; }
      const body2 = (await r2.json()) as UnlockResponse;
      if (body2.token) setToken(body2.token);
      resolve();
    };
  });
}

function renderLocked(container: HTMLElement): Promise<void> {
  return new Promise((resolve) => {
    container.innerHTML = `
      <div class="lock-card">
        <h1>JARVIS</h1>
        <p>Enter your passphrase.</p>
        <input type="password" id="lock-pp" placeholder="passphrase" autofocus />
        <div class="lock-err" id="lock-err"></div>
        <button id="lock-submit">Unlock</button>
      </div>`;
    const btn = container.querySelector("#lock-submit") as HTMLButtonElement;
    const tryUnlock = async () => {
      const pp = (container.querySelector("#lock-pp") as HTMLInputElement).value;
      const err = container.querySelector("#lock-err") as HTMLElement;
      err.textContent = "Unlocking…";
      const r = await postJson("/api/auth/unlock", { passphrase: pp });
      if (r.status === 401) { err.textContent = "Wrong passphrase"; return; }
      if (r.status === 429) { err.textContent = "Slow down — too many attempts"; return; }
      if (!r.ok) { err.textContent = `Error ${r.status}`; return; }
      const body = (await r.json()) as UnlockResponse;
      if (body.token) setToken(body.token);
      resolve();
    };
    btn.onclick = tryUnlock;
    (container.querySelector("#lock-pp") as HTMLInputElement).onkeydown = (e) => {
      if (e.key === "Enter") tryUnlock();
    };
  });
}

export async function awaitUnlock(): Promise<void> {
  const container = document.getElementById("lock-screen");
  if (!container) throw new Error("missing #lock-screen container");
  container.style.display = "block";
  const state = await fetchState();
  if (!state.initialized) {
    await renderFirstRun(container);
  } else if (state.locked) {
    await renderLocked(container);
  } else if (!getToken()) {
    // Vault is unlocked server-side, but THIS browser tab has no token —
    // typically because the page was reloaded after the original unlock.
    // Show the unlock prompt so the user can re-issue (Argon2id will re-run,
    // but the alternative is a silently-broken UI).
    await renderLocked(container);
  }
  container.style.display = "none";
  container.innerHTML = "";
}
