/**
 * Documents panel — minimal UI for Aria's document store (B.3 phase 1).
 *
 * Self-contained: own modal DOM, own styles (inline so it doesn't depend
 * on style.css). Uses the existing /api/documents endpoints.
 *
 * Pattern: open/close pair, list+create+delete in one panel. View-content
 * is intentionally NOT a separate screen — clicking a list row expands
 * the body inline so the user can re-check what Aria has on her end.
 */

import { withAuthHeaders } from "./auth-token";

interface DocumentMeta {
  id: number;
  title: string;
  content_bytes: number;
  created_at: number;
  updated_at: number;
}

interface DocumentFull extends DocumentMeta {
  content: string;
}

let panelEl: HTMLDivElement | null = null;
let isOpen = false;

const MAX_BYTES = 50 * 1024; // mirrors aria_documents.MAX_CONTENT_BYTES

// ---------------------------------------------------------------------------
// Styles (injected once on first open)
// ---------------------------------------------------------------------------

const STYLE_ID = "aria-documents-panel-styles";

function injectStyles() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    #aria-documents-panel {
      position: fixed; inset: 0;
      background: rgba(5, 5, 10, 0.85);
      backdrop-filter: blur(6px);
      z-index: 9999;
      display: none;
      align-items: center; justify-content: center;
      opacity: 0; transition: opacity 220ms ease;
    }
    #aria-documents-panel.open { opacity: 1; }
    #aria-documents-panel .docs-modal {
      width: min(720px, 92vw);
      max-height: 86vh;
      background: #0c0c14;
      border: 1px solid #1f2030;
      border-radius: 12px;
      color: #e6e8ef;
      display: flex; flex-direction: column;
      box-shadow: 0 30px 80px rgba(0,0,0,0.6);
      overflow: hidden;
    }
    #aria-documents-panel .docs-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 14px 18px; border-bottom: 1px solid #1f2030;
    }
    #aria-documents-panel .docs-header h2 {
      margin: 0; font-size: 14px; font-weight: 600;
      letter-spacing: 0.04em; text-transform: uppercase;
      color: #9aa3b8;
    }
    #aria-documents-panel .docs-close {
      background: transparent; border: 0; color: #9aa3b8;
      font-size: 18px; line-height: 1; cursor: pointer; padding: 4px 8px;
    }
    #aria-documents-panel .docs-close:hover { color: #fff; }
    #aria-documents-panel .docs-body {
      padding: 16px 18px; overflow-y: auto; flex: 1;
      display: flex; flex-direction: column; gap: 18px;
    }
    #aria-documents-panel .docs-section-label {
      font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase;
      color: #6b7385; margin-bottom: 6px;
    }
    #aria-documents-panel input[type="text"],
    #aria-documents-panel textarea {
      width: 100%; box-sizing: border-box;
      background: #14141f; border: 1px solid #232438;
      color: #e6e8ef; border-radius: 6px;
      padding: 8px 10px; font-size: 13px;
      font-family: ui-monospace, monospace;
    }
    #aria-documents-panel input[type="text"]:focus,
    #aria-documents-panel textarea:focus {
      outline: none; border-color: #38bdf8;
    }
    #aria-documents-panel textarea {
      min-height: 180px; resize: vertical; line-height: 1.4;
    }
    #aria-documents-panel .docs-meta {
      font-size: 11px; color: #6b7385; margin-top: 4px;
    }
    #aria-documents-panel .docs-meta.warn { color: #f59e0b; }
    #aria-documents-panel .docs-meta.error { color: #ef4444; }
    #aria-documents-panel .docs-btn {
      background: #1f6feb; color: #fff; border: 0;
      border-radius: 6px; padding: 8px 14px; font-size: 13px;
      cursor: pointer; font-weight: 500;
    }
    #aria-documents-panel .docs-btn:hover { background: #2384ff; }
    #aria-documents-panel .docs-btn:disabled {
      background: #2a2d3a; color: #6b7385; cursor: not-allowed;
    }
    #aria-documents-panel .docs-list { list-style: none; padding: 0; margin: 0; }
    #aria-documents-panel .docs-list li {
      border-top: 1px solid #1f2030; padding: 10px 0;
    }
    #aria-documents-panel .docs-list li:first-child { border-top: 0; }
    #aria-documents-panel .docs-list .row {
      display: flex; align-items: center; justify-content: space-between;
      gap: 10px; cursor: pointer;
    }
    #aria-documents-panel .docs-list .title {
      font-size: 14px; color: #e6e8ef; flex: 1; min-width: 0;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    #aria-documents-panel .docs-list .sub {
      font-size: 11px; color: #6b7385; white-space: nowrap;
    }
    #aria-documents-panel .docs-list .delete {
      background: transparent; border: 0; color: #ef4444;
      cursor: pointer; padding: 4px 6px; font-size: 12px;
    }
    #aria-documents-panel .docs-list .delete:hover { color: #ff6868; }
    #aria-documents-panel .docs-list .expanded {
      margin-top: 8px; background: #07070d;
      border: 1px solid #1f2030; border-radius: 6px;
      padding: 10px; font-size: 12px; font-family: ui-monospace, monospace;
      white-space: pre-wrap; word-break: break-word; max-height: 240px;
      overflow-y: auto; color: #c3c9d8;
    }
    #aria-documents-panel .docs-empty {
      color: #6b7385; font-size: 13px; text-align: center; padding: 12px;
    }
  `;
  document.head.appendChild(style);
}

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

function createPanel(): HTMLDivElement {
  const root = document.createElement("div");
  root.id = "aria-documents-panel";
  root.innerHTML = `
    <div class="docs-modal" role="dialog" aria-modal="true" aria-label="Documents">
      <div class="docs-header">
        <h2>Documents — what Aria can read</h2>
        <button class="docs-close" aria-label="Close">&times;</button>
      </div>
      <div class="docs-body">
        <div>
          <div class="docs-section-label">Add a document</div>
          <input id="docs-title" type="text" placeholder="Title (e.g. Acme term sheet)" maxlength="200" />
          <div style="height: 8px;"></div>
          <textarea id="docs-content" placeholder="Paste the document body (text or markdown). She'll see this whenever you mention the title in conversation."></textarea>
          <div id="docs-meta" class="docs-meta">0 bytes</div>
          <div style="height: 10px;"></div>
          <button id="docs-add" class="docs-btn">Add document</button>
        </div>
        <div>
          <div class="docs-section-label">Stored documents</div>
          <ul id="docs-list" class="docs-list"></ul>
          <div id="docs-empty" class="docs-empty" style="display:none">No documents stored yet.</div>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(root);
  wireEvents(root);
  return root;
}

function wireEvents(root: HTMLDivElement) {
  const closeBtn = root.querySelector(".docs-close") as HTMLButtonElement;
  closeBtn.addEventListener("click", () => closeDocumentsPanel());
  root.addEventListener("click", (e) => {
    if (e.target === root) closeDocumentsPanel();
  });

  const contentEl = root.querySelector("#docs-content") as HTMLTextAreaElement;
  const metaEl = root.querySelector("#docs-meta") as HTMLDivElement;
  const addBtn = root.querySelector("#docs-add") as HTMLButtonElement;
  const titleEl = root.querySelector("#docs-title") as HTMLInputElement;

  function updateMeta() {
    const bytes = new TextEncoder().encode(contentEl.value).length;
    metaEl.textContent = `${bytes.toLocaleString()} / ${MAX_BYTES.toLocaleString()} bytes`;
    if (bytes > MAX_BYTES) {
      metaEl.classList.add("error");
      metaEl.classList.remove("warn");
      addBtn.disabled = true;
    } else if (bytes > MAX_BYTES * 0.9) {
      metaEl.classList.add("warn");
      metaEl.classList.remove("error");
      addBtn.disabled = false;
    } else {
      metaEl.classList.remove("warn", "error");
      addBtn.disabled = false;
    }
  }
  contentEl.addEventListener("input", updateMeta);
  updateMeta();

  addBtn.addEventListener("click", async () => {
    const title = titleEl.value.trim();
    const content = contentEl.value;
    if (!title) {
      titleEl.focus();
      return;
    }
    addBtn.disabled = true;
    const orig = addBtn.textContent;
    addBtn.textContent = "Adding…";
    try {
      const r = await fetch("/api/documents", withAuthHeaders({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, content }),
      }));
      if (!r.ok) {
        const detail = await r.json().catch(() => ({ detail: "unknown error" }));
        metaEl.textContent = `Error: ${detail.detail ?? r.status}`;
        metaEl.classList.add("error");
      } else {
        titleEl.value = "";
        contentEl.value = "";
        updateMeta();
        await refreshList(root);
      }
    } catch (err) {
      metaEl.textContent = `Error: ${err}`;
      metaEl.classList.add("error");
    } finally {
      addBtn.disabled = false;
      addBtn.textContent = orig;
    }
  });
}

async function refreshList(root: HTMLDivElement) {
  const listEl = root.querySelector("#docs-list") as HTMLUListElement;
  const emptyEl = root.querySelector("#docs-empty") as HTMLDivElement;
  let docs: DocumentMeta[] = [];
  try {
    const r = await fetch("/api/documents", withAuthHeaders());
    if (r.ok) {
      const payload = await r.json();
      docs = payload.documents || [];
    }
  } catch (_err) { /* silent — show empty */ }

  listEl.innerHTML = "";
  if (docs.length === 0) {
    emptyEl.style.display = "block";
    return;
  }
  emptyEl.style.display = "none";

  for (const d of docs) {
    const li = document.createElement("li");
    const created = new Date(d.created_at * 1000);
    const dateStr = created.toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
    const sizeKb = (d.content_bytes / 1024).toFixed(1);
    li.innerHTML = `
      <div class="row">
        <div class="title">${escapeHtml(d.title)}</div>
        <div class="sub">${sizeKb} KB · ${dateStr}</div>
        <button class="delete" aria-label="Delete">Delete</button>
      </div>
    `;
    const row = li.querySelector(".row") as HTMLDivElement;
    const delBtn = li.querySelector(".delete") as HTMLButtonElement;
    let expandedEl: HTMLDivElement | null = null;

    row.addEventListener("click", async (e) => {
      if (e.target === delBtn) return;
      if (expandedEl) {
        expandedEl.remove();
        expandedEl = null;
        return;
      }
      try {
        const r = await fetch(`/api/documents/${d.id}`, withAuthHeaders());
        if (!r.ok) return;
        const full: DocumentFull = await r.json();
        expandedEl = document.createElement("div");
        expandedEl.className = "expanded";
        expandedEl.textContent = full.content;
        li.appendChild(expandedEl);
      } catch (_err) { /* swallow */ }
    });

    delBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete "${d.title}"? Aria will lose access to this document.`)) return;
      try {
        await fetch(`/api/documents/${d.id}`, withAuthHeaders({ method: "DELETE" }));
        await refreshList(root);
      } catch (_err) { /* swallow */ }
    });

    listEl.appendChild(li);
  }
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function openDocumentsPanel() {
  if (isOpen) return;
  injectStyles();
  if (!panelEl) panelEl = createPanel();
  isOpen = true;
  panelEl.style.display = "flex";
  requestAnimationFrame(() => panelEl!.classList.add("open"));
  await refreshList(panelEl);
}

export function closeDocumentsPanel() {
  if (!panelEl || !isOpen) return;
  isOpen = false;
  panelEl.classList.remove("open");
  setTimeout(() => {
    if (panelEl) panelEl.style.display = "none";
  }, 220);
}

export function isDocumentsPanelOpen(): boolean {
  return isOpen;
}
