/**
 * Single source of truth for the JARVIS auth token across the frontend.
 *
 * The token is delivered by /api/auth/unlock after the user enters their
 * passphrase. It must be attached to every subsequent /api/* fetch as
 * `X-JARVIS-Token`, and to every /ws/* WebSocket as `?token=...`.
 *
 * Docker note: the backend's loopback-bypass doesn't fire when the
 * container's client IP is the bridge gateway (not 127.0.0.1). So the
 * token is required even on a local dev setup.
 */

/**
 * Token storage: sessionStorage.
 *
 * Why sessionStorage (not localStorage, not in-memory-only):
 * - localStorage persists across browser sessions and may outlive the
 *   server process — undesirable if the user closes the laptop, hands
 *   it briefly to someone, and someone reopens the browser.
 * - In-memory-only forces a re-prompt on every refresh. Argon2id KDF
 *   is multi-second per attempt; that's punitive for an F5.
 * - sessionStorage survives refresh, dies on tab close, scoped to the
 *   origin. Aligns with SECURITY.md threat model (single-user Mac,
 *   FileVault as the at-rest defense; "user with shell on host" is
 *   explicitly NOT defended against).
 */
const STORAGE_KEY = "jarvis.auth_token";

function readStored(): string {
  try {
    return sessionStorage.getItem(STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

let _token = readStored();

export function setToken(t: string): void {
  _token = t || "";
  try {
    if (_token) sessionStorage.setItem(STORAGE_KEY, _token);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* sessionStorage unavailable (privacy mode, etc.) — fall back to memory-only */
  }
}

export function getToken(): string {
  return _token;
}

export function clearToken(): void {
  setToken("");
}

/** Wrap a header object with the token if we have one. */
export function withAuthHeaders(init: RequestInit = {}): RequestInit {
  if (!_token) return init;
  const headers = new Headers(init.headers || {});
  headers.set("X-JARVIS-Token", _token);
  return { ...init, headers };
}

/** Append the token as a query param to a WebSocket URL. */
export function withAuthQuery(url: string): string {
  if (!_token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(_token)}`;
}
