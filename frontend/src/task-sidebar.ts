/**
 * JARVIS — Task Sidebar
 *
 * Left-edge panel showing what JARVIS is working on: project dispatches with
 * live status. Populates from:
 *   - GET /api/dispatches on load (restores recent state after refresh)
 *   - {type: "dispatch", id, project, status, summary, url, ts} WS events
 *
 * Both sources use the SAME event shape, so there's a single render path.
 * Cards are upserted by dispatch id.
 */

import type { JarvisSocket } from "./ws";
import { withAuthHeaders } from "./auth-token";
import "./task-sidebar.css";

interface DispatchEvent {
  id: number;
  project: string;
  status: string; // pending | building | completed | failed | timeout
  summary: string;
  url: string | null;
  ts: number;
}

const STATUS_LABEL: Record<string, string> = {
  pending: "Queued",
  building: "Working",
  planning: "Planning",
  completed: "Done",
  failed: "Failed",
  timeout: "Timed out",
};

let listEl: HTMLElement | null = null;
let emptyEl: HTMLElement | null = null;
const cards = new Map<number, HTMLElement>();

function statusClass(status: string): string {
  if (status === "completed") return "ok";
  if (status === "failed" || status === "timeout") return "err";
  if (status === "pending") return "queued";
  return "active"; // building / planning
}

function upsert(d: DispatchEvent): void {
  if (!listEl) return;

  if (emptyEl && emptyEl.parentElement) {
    emptyEl.remove();
    emptyEl = null;
  }

  let card = cards.get(d.id);
  if (!card) {
    card = document.createElement("div");
    card.className = "task-card";
    // Newest on top.
    listEl.prepend(card);
    cards.set(d.id, card);
  }

  const label = STATUS_LABEL[d.status] || d.status;
  // URL is escaped on both sides defensively. The backend regex already
  // restricts the value to https?:// with no whitespace or quotes, but the
  // summary text it's pulled from can in principle be attacker-influenced
  // (it flows from LLM responses into the dispatch record).
  const safeUrl = d.url ? escapeHtml(d.url) : "";
  const urlHtml = d.url
    ? `<a class="task-url" href="${safeUrl}" target="_blank" rel="noopener">${safeUrl}</a>`
    : "";
  const summaryHtml = d.summary
    ? `<div class="task-summary">${escapeHtml(d.summary)}</div>`
    : "";

  card.innerHTML = `
    <div class="task-card-head">
      <span class="task-project">${escapeHtml(d.project || "(untitled)")}</span>
      <span class="task-pill ${statusClass(d.status)}">${escapeHtml(label)}</span>
    </div>
    ${summaryHtml}
    ${urlHtml}
  `;
}

function escapeHtml(s: string): string {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

async function loadRecent(): Promise<void> {
  try {
    const res = await fetch("/api/dispatches", withAuthHeaders());
    if (!res.ok) return;
    const data = (await res.json()) as { dispatches?: DispatchEvent[] };
    // API returns newest-first; render oldest-first so prepend keeps newest on top.
    const items = (data.dispatches || []).slice().reverse();
    for (const d of items) upsert(d);
  } catch {
    /* offline / locked — sidebar stays empty until live events arrive */
  }
}

export function attachTaskSidebar(socket: JarvisSocket): void {
  const panel = document.getElementById("task-sidebar");
  if (!panel) {
    console.warn("[tasks] #task-sidebar not found in DOM");
    return;
  }

  const header = document.createElement("div");
  header.className = "task-header";
  header.textContent = "Tasks";

  listEl = document.createElement("div");
  listEl.id = "task-list";
  listEl.setAttribute("aria-label", "Active and recent tasks");

  emptyEl = document.createElement("div");
  emptyEl.className = "task-empty";
  emptyEl.textContent = "No tasks yet. Ask JARVIS to build or work on a project.";
  listEl.appendChild(emptyEl);

  panel.appendChild(header);
  panel.appendChild(listEl);

  socket.onMessage((msg) => {
    if (msg.type === "dispatch") {
      upsert(msg as unknown as DispatchEvent);
    }
  });

  void loadRecent();
}

export function toggleTaskSidebar(): boolean {
  const panel = document.getElementById("task-sidebar");
  if (!panel) return false;
  const isHidden = panel.classList.toggle("hidden");
  return !isHidden;
}
