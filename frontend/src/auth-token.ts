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

let _token = "";

export function setToken(t: string): void {
  _token = t || "";
}

export function getToken(): string {
  return _token;
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
