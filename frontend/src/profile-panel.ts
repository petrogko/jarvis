/**
 * Profile panel — view + edit what Aria persistently knows about him.
 *
 * The profile is a single markdown document Aria reads every turn. She
 * appends notes via [PROFILE_NOTE: ...] markers; this UI lets the user
 * see what's accumulated, prune wrong notes, and add stable facts
 * manually that she may not have surfaced yet.
 *
 * Every save snapshots the prior content on the server side so bad
 * edits can be recovered (history table, capped at 20 snapshots).
 */

import { withAuthHeaders } from "./auth-token";

let panelEl: HTMLDivElement | null = null;
let isOpen = false;
let lastLoadedContent = ""; // for dirty-check

const STYLE_ID = "aria-profile-panel-styles";

function injectStyles() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    #aria-profile-panel {
      position: fixed; inset: 0;
      background: rgba(5, 5, 10, 0.85);
      backdrop-filter: blur(6px);
      z-index: 9999;
      display: none;
      align-items: center; justify-content: center;
      opacity: 0; transition: opacity 220ms ease;
    }
    #aria-profile-panel.open { opacity: 1; }
    #aria-profile-panel .profile-modal {
      width: min(820px, 94vw);
      max-height: 88vh;
      background: #0c0c14;
      border: 1px solid #1f2030;
      border-radius: 12px;
      color: #e6e8ef;
      display: flex; flex-direction: column;
      box-shadow: 0 30px 80px rgba(0,0,0,0.6);
      overflow: hidden;
    }
    #aria-profile-panel .profile-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 14px 18px; border-bottom: 1px solid #1f2030;
    }
    #aria-profile-panel .profile-header h2 {
      margin: 0; font-size: 14px; font-weight: 600;
      letter-spacing: 0.04em; text-transform: uppercase;
      color: #9aa3b8;
    }
    #aria-profile-panel .profile-header .sub {
      font-size: 11px; color: #6b7385; margin-top: 4px;
    }
    #aria-profile-panel .profile-close {
      background: transparent; border: 0; color: #9aa3b8;
      font-size: 18px; line-height: 1; cursor: pointer; padding: 4px 8px;
    }
    #aria-profile-panel .profile-close:hover { color: #fff; }
    #aria-profile-panel .profile-body {
      padding: 14px 18px; overflow-y: auto; flex: 1;
      display: flex; flex-direction: column; gap: 12px;
    }
    #aria-profile-panel textarea {
      width: 100%; box-sizing: border-box; flex: 1;
      min-height: 380px;
      background: #14141f; border: 1px solid #232438;
      color: #e6e8ef; border-radius: 6px;
      padding: 10px 12px; font-size: 13px;
      font-family: ui-monospace, monospace;
      resize: vertical; line-height: 1.5;
    }
    #aria-profile-panel textarea:focus {
      outline: none; border-color: #38bdf8;
    }
    #aria-profile-panel .profile-footer {
      display: flex; align-items: center; justify-content: space-between;
      gap: 10px; padding: 12px 18px; border-top: 1px solid #1f2030;
    }
    #aria-profile-panel .profile-meta { font-size: 11px; color: #6b7385; }
    #aria-profile-panel .profile-meta.dirty { color: #f59e0b; }
    #aria-profile-panel .profile-meta.error { color: #ef4444; }
    #aria-profile-panel .profile-meta.saved { color: #4ade80; }
    #aria-profile-panel .profile-btn-row { display: flex; gap: 8px; }
    #aria-profile-panel .profile-btn {
      background: #1f6feb; color: #fff; border: 0;
      border-radius: 6px; padding: 8px 14px; font-size: 13px;
      cursor: pointer; font-weight: 500;
    }
    #aria-profile-panel .profile-btn:hover { background: #2384ff; }
    #aria-profile-panel .profile-btn.secondary {
      background: transparent; color: #9aa3b8; border: 1px solid #232438;
    }
    #aria-profile-panel .profile-btn.secondary:hover {
      background: #14141f; color: #fff;
    }
    #aria-profile-panel .profile-btn:disabled {
      background: #2a2d3a; color: #6b7385; cursor: not-allowed;
    }
    #aria-profile-panel .history-list {
      list-style: none; padding: 0; margin: 0;
      max-height: 120px; overflow-y: auto;
    }
    #aria-profile-panel .history-list li {
      padding: 6px 8px; border-radius: 4px; font-size: 12px;
      color: #c3c9d8; cursor: pointer; display: flex; justify-content: space-between;
    }
    #aria-profile-panel .history-list li:hover { background: #14141f; }
    #aria-profile-panel .history-empty {
      color: #6b7385; font-size: 11px; padding: 4px 8px;
    }
  `;
  document.head.appendChild(style);
}

function createPanel(): HTMLDivElement {
  const root = document.createElement("div");
  root.id = "aria-profile-panel";
  root.innerHTML = `
    <div class="profile-modal" role="dialog" aria-modal="true" aria-label="Profile">
      <div class="profile-header">
        <div>
          <h2>What Aria knows about you</h2>
          <div class="sub">Edit freely. She reads this on every turn. Each save snapshots the prior version.</div>
        </div>
        <button class="profile-close" aria-label="Close">&times;</button>
      </div>
      <div class="profile-body">
        <textarea id="profile-content" spellcheck="false" placeholder="(Aria will fill this in as she gets to know you. You can also edit directly.)"></textarea>
        <div>
          <div style="font-size: 11px; color: #6b7385; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px;">
            Recent snapshots (click to view)
          </div>
          <ul id="profile-history" class="history-list"></ul>
          <div id="profile-history-empty" class="history-empty" style="display:none">No snapshots yet — they're created on every save.</div>
        </div>
      </div>
      <div class="profile-footer">
        <div id="profile-meta" class="profile-meta">0 bytes</div>
        <div class="profile-btn-row">
          <button id="profile-revert" class="profile-btn secondary">Revert</button>
          <button id="profile-save" class="profile-btn" disabled>Save</button>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(root);
  wireEvents(root);
  return root;
}

function wireEvents(root: HTMLDivElement) {
  const closeBtn = root.querySelector(".profile-close") as HTMLButtonElement;
  closeBtn.addEventListener("click", () => closeProfilePanel());
  root.addEventListener("click", (e) => {
    if (e.target === root) closeProfilePanel();
  });

  const textarea = root.querySelector("#profile-content") as HTMLTextAreaElement;
  const metaEl = root.querySelector("#profile-meta") as HTMLDivElement;
  const saveBtn = root.querySelector("#profile-save") as HTMLButtonElement;
  const revertBtn = root.querySelector("#profile-revert") as HTMLButtonElement;

  function updateMeta() {
    const bytes = new TextEncoder().encode(textarea.value).length;
    const dirty = textarea.value !== lastLoadedContent;
    metaEl.classList.remove("dirty", "error", "saved");
    if (dirty) metaEl.classList.add("dirty");
    metaEl.textContent = dirty
      ? `${bytes.toLocaleString()} bytes — unsaved changes`
      : `${bytes.toLocaleString()} bytes`;
    saveBtn.disabled = !dirty;
  }
  textarea.addEventListener("input", updateMeta);

  revertBtn.addEventListener("click", () => {
    textarea.value = lastLoadedContent;
    updateMeta();
  });

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    const origText = saveBtn.textContent;
    saveBtn.textContent = "Saving…";
    try {
      const r = await fetch("/api/profile", withAuthHeaders({
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: textarea.value }),
      }));
      if (!r.ok) {
        const detail = await r.json().catch(() => ({ detail: "unknown error" }));
        metaEl.classList.remove("dirty");
        metaEl.classList.add("error");
        metaEl.textContent = `Error: ${detail.detail ?? r.status}`;
      } else {
        lastLoadedContent = textarea.value;
        metaEl.classList.remove("dirty");
        metaEl.classList.add("saved");
        const bytes = new TextEncoder().encode(textarea.value).length;
        metaEl.textContent = `${bytes.toLocaleString()} bytes — saved`;
        saveBtn.disabled = true;
        await refreshHistory(root);
      }
    } catch (err) {
      metaEl.classList.add("error");
      metaEl.textContent = `Error: ${err}`;
    } finally {
      saveBtn.textContent = origText;
    }
  });
}

async function loadProfile(root: HTMLDivElement) {
  const textarea = root.querySelector("#profile-content") as HTMLTextAreaElement;
  const metaEl = root.querySelector("#profile-meta") as HTMLDivElement;
  try {
    const r = await fetch("/api/profile", withAuthHeaders());
    if (!r.ok) {
      metaEl.classList.add("error");
      metaEl.textContent = `Error loading: ${r.status}`;
      return;
    }
    const payload = await r.json();
    lastLoadedContent = payload.content || "";
    textarea.value = lastLoadedContent;
    metaEl.classList.remove("dirty", "error", "saved");
    metaEl.textContent = `${(payload.bytes ?? 0).toLocaleString()} bytes`;
    (root.querySelector("#profile-save") as HTMLButtonElement).disabled = true;
  } catch (err) {
    metaEl.classList.add("error");
    metaEl.textContent = `Error: ${err}`;
  }
}

async function refreshHistory(root: HTMLDivElement) {
  const listEl = root.querySelector("#profile-history") as HTMLUListElement;
  const emptyEl = root.querySelector("#profile-history-empty") as HTMLDivElement;
  let snapshots: Array<{ id: number; created_at: number; bytes: number }> = [];
  try {
    const r = await fetch("/api/profile/history", withAuthHeaders());
    if (r.ok) {
      const payload = await r.json();
      snapshots = payload.snapshots || [];
    }
  } catch (_err) { /* silent */ }

  listEl.innerHTML = "";
  if (snapshots.length === 0) {
    emptyEl.style.display = "block";
    return;
  }
  emptyEl.style.display = "none";
  for (const s of snapshots) {
    const li = document.createElement("li");
    const when = new Date(s.created_at * 1000).toLocaleString();
    li.innerHTML = `<span>${when}</span><span style="color:#6b7385">${(s.bytes / 1024).toFixed(1)} KB</span>`;
    li.addEventListener("click", async () => {
      try {
        const r = await fetch(`/api/profile/history/${s.id}`, withAuthHeaders());
        if (!r.ok) return;
        const payload = await r.json();
        // Show in an alert so user can copy if they want to restore.
        // (A proper "restore this version" button is a follow-up.)
        const ok = confirm(
          `Snapshot from ${when}\n\n` +
          `Click OK to load it into the editor (you can then save it to restore).\n` +
          `Click Cancel to do nothing.\n\n` +
          `Preview:\n${(payload.content || "").slice(0, 400)}…`
        );
        if (ok) {
          const textarea = root.querySelector("#profile-content") as HTMLTextAreaElement;
          textarea.value = payload.content || "";
          textarea.dispatchEvent(new Event("input"));
        }
      } catch (_err) { /* swallow */ }
    });
    listEl.appendChild(li);
  }
}

export async function openProfilePanel() {
  if (isOpen) return;
  injectStyles();
  if (!panelEl) panelEl = createPanel();
  isOpen = true;
  panelEl.style.display = "flex";
  requestAnimationFrame(() => panelEl!.classList.add("open"));
  await loadProfile(panelEl);
  await refreshHistory(panelEl);
}

export function closeProfilePanel() {
  if (!panelEl || !isOpen) return;
  isOpen = false;
  panelEl.classList.remove("open");
  setTimeout(() => {
    if (panelEl) panelEl.style.display = "none";
  }, 220);
}

export function isProfilePanelOpen(): boolean {
  return isOpen;
}
