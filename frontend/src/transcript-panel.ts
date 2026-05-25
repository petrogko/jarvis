/**
 * JARVIS — Debug Transcript Panel
 *
 * Renders a scrollable conversation log on the right side of the UI.
 * Populates from incoming WebSocket messages:
 *   - {type: "transcript", text, isFinal} → USER line (isFinal only)
 *   - {type: "audio", text}               → JARVIS line
 *   - {type: "status", state}             → STATUS line (faded)
 */

import type { JarvisSocket } from "./ws";
import "./transcript-panel.css";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function timestamp(): string {
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, "0");
  const mm = String(now.getMinutes()).padStart(2, "0");
  const ss = String(now.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

// ---------------------------------------------------------------------------
// Transcript state
// ---------------------------------------------------------------------------

let logEl: HTMLElement | null = null;
let emptyEl: HTMLElement | null = null;

/**
 * Returns true when the user is scrolled near the bottom (within 80px),
 * meaning we should auto-scroll on new entries.
 */
function isNearBottom(): boolean {
  if (!logEl) return true;
  return logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 80;
}

function scrollToBottom() {
  if (logEl) {
    logEl.scrollTop = logEl.scrollHeight;
  }
}

type Speaker = "user" | "jarvis" | "status";

function appendEntry(speaker: Speaker, text: string) {
  if (!logEl) return;

  // Remove empty state placeholder on first entry
  if (emptyEl && emptyEl.parentElement) {
    emptyEl.remove();
    emptyEl = null;
  }

  const shouldScroll = isNearBottom();

  const entry = document.createElement("div");
  entry.className = `transcript-entry ${speaker}`;

  const tsEl = document.createElement("span");
  tsEl.className = "transcript-ts";
  tsEl.textContent = timestamp();

  const bodyEl = document.createElement("span");
  bodyEl.className = "transcript-body";

  const labelEl = document.createElement("span");
  labelEl.className = "transcript-speaker";
  labelEl.textContent =
    speaker === "user" ? "You" : speaker === "jarvis" ? "JARVIS" : "•";

  const textEl = document.createElement("span");
  textEl.className = "transcript-text";
  textEl.textContent = text;

  bodyEl.appendChild(labelEl);
  bodyEl.appendChild(textEl);
  entry.appendChild(tsEl);
  entry.appendChild(bodyEl);
  logEl.appendChild(entry);

  if (shouldScroll) {
    scrollToBottom();
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function attachTranscript(socket: JarvisSocket): void {
  const panel = document.getElementById("transcript-panel");
  if (!panel) {
    console.warn("[transcript] #transcript-panel not found in DOM");
    return;
  }

  // Build interior structure
  const header = document.createElement("div");
  header.className = "transcript-header";
  header.textContent = "Conversation";

  logEl = document.createElement("div");
  logEl.id = "transcript-log";
  logEl.setAttribute("aria-label", "Conversation transcript");

  emptyEl = document.createElement("div");
  emptyEl.className = "transcript-empty";
  emptyEl.textContent = "Speak to start a conversation…";
  logEl.appendChild(emptyEl);

  panel.appendChild(header);
  panel.appendChild(logEl);

  // ---------------------------------------------------------------------------
  // Wire WebSocket messages
  // ---------------------------------------------------------------------------

  socket.onMessage((msg) => {
    const type = msg.type as string;

    if (type === "transcript") {
      // User speech — only log final results
      if (msg.isFinal && msg.text) {
        appendEntry("user", msg.text as string);
      }
    } else if (type === "audio") {
      // JARVIS speech — text field carries the spoken text
      if (msg.text) {
        appendEntry("jarvis", msg.text as string);
      }
    } else if (type === "status") {
      // State transitions — optional faded status line
      const state = msg.state as string;
      if (state === "thinking") {
        appendEntry("status", "thinking…");
      } else if (state === "working") {
        appendEntry("status", "working…");
      }
      // "idle" / "speaking" are visual-only — skip cluttering the log
    }
  });
}

// ---------------------------------------------------------------------------
// Imperative push helpers — called by send-sites that can't use onMessage
// (outgoing messages are never echoed back, so the WS listener misses them)
// ---------------------------------------------------------------------------

/**
 * Push a USER line into the transcript immediately after socket.send().
 * Call this from text-input.ts and the voice-input handler in main.ts.
 */
export function pushUserLine(text: string): void {
  appendEntry("user", text);
}

// ---------------------------------------------------------------------------
// Toggle helper (called from main.ts menu button)
// ---------------------------------------------------------------------------

export function toggleTranscript(): boolean {
  const panel = document.getElementById("transcript-panel");
  if (!panel) return false;

  const isHidden = panel.classList.toggle("hidden");
  return !isHidden; // returns true if now visible
}
