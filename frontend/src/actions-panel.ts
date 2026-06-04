/**
 * Actions panel — long-running external-action tracker (Pine-AI-style work).
 *
 * v1: call_draft only. Aria emits [ACTION:CALL_DRAFT] from conversation;
 * server creates a record, dispatches Sonnet to draft a structured script,
 * and stores the plan. This panel shows the open drafts, lets the user
 * read the plan, mark in-progress when they place the call, and record
 * the outcome (notes + optional dollar amount) when done.
 */

import { withAuthHeaders } from "./auth-token";

interface ActionRecord {
  id: number;
  kind: string;
  status: "drafted" | "in_progress" | "completed" | "abandoned";
  vendor: string;
  goal: string;
  phone: string;
  plan: string;
  outcome_notes: string;
  outcome_value_cents: number | null;
  created_at: number;
  updated_at: number;
  completed_at: number | null;
}

let panelEl: HTMLDivElement | null = null;
let isOpen = false;
let pollTimer: number | null = null;

const STYLE_ID = "aria-actions-panel-styles";

function injectStyles() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    #aria-actions-panel {
      position: fixed; inset: 0;
      background: rgba(5, 5, 10, 0.85);
      backdrop-filter: blur(6px);
      z-index: 9999;
      display: none;
      align-items: center; justify-content: center;
      opacity: 0; transition: opacity 220ms ease;
    }
    #aria-actions-panel.open { opacity: 1; }
    #aria-actions-panel .actions-modal {
      width: min(860px, 95vw);
      max-height: 88vh;
      background: #0c0c14;
      border: 1px solid #1f2030;
      border-radius: 12px;
      color: #e6e8ef;
      display: flex; flex-direction: column;
      box-shadow: 0 30px 80px rgba(0,0,0,0.6);
      overflow: hidden;
    }
    #aria-actions-panel .actions-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 14px 18px; border-bottom: 1px solid #1f2030;
    }
    #aria-actions-panel .actions-header h2 {
      margin: 0; font-size: 14px; font-weight: 600;
      letter-spacing: 0.04em; text-transform: uppercase;
      color: #9aa3b8;
    }
    #aria-actions-panel .actions-header .sub {
      font-size: 11px; color: #6b7385; margin-top: 4px;
    }
    #aria-actions-panel .actions-close {
      background: transparent; border: 0; color: #9aa3b8;
      font-size: 18px; line-height: 1; cursor: pointer; padding: 4px 8px;
    }
    #aria-actions-panel .actions-close:hover { color: #fff; }
    #aria-actions-panel .actions-body {
      padding: 12px 18px; overflow-y: auto; flex: 1;
      display: flex; flex-direction: column; gap: 12px;
    }
    #aria-actions-panel .action-card {
      border: 1px solid #1f2030; border-radius: 8px;
      padding: 12px 14px; background: #0e0e16;
    }
    #aria-actions-panel .action-card.in_progress { border-color: #2384ff; }
    #aria-actions-panel .action-card.completed { opacity: 0.65; border-color: #284b30; }
    #aria-actions-panel .action-card.abandoned { opacity: 0.45; }
    #aria-actions-panel .action-header {
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
    }
    #aria-actions-panel .action-title { font-size: 14px; flex: 1; min-width: 0; }
    #aria-actions-panel .action-vendor { color: #9aa3b8; }
    #aria-actions-panel .action-goal { color: #c3c9d8; }
    #aria-actions-panel .status-badge {
      font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase;
      padding: 2px 8px; border-radius: 10px; white-space: nowrap;
    }
    #aria-actions-panel .status-badge.drafted { background: #1c2331; color: #9aa3b8; }
    #aria-actions-panel .status-badge.in_progress { background: #173158; color: #7ec3ff; }
    #aria-actions-panel .status-badge.completed { background: #1d3a23; color: #7eda9b; }
    #aria-actions-panel .status-badge.abandoned { background: #3a1d1d; color: #ff9999; }
    #aria-actions-panel .action-meta {
      font-size: 11px; color: #6b7385; margin-top: 4px;
    }
    #aria-actions-panel .action-plan {
      margin-top: 10px; background: #07070d;
      border: 1px solid #1f2030; border-radius: 6px;
      padding: 10px 12px; font-size: 12px; font-family: ui-monospace, monospace;
      white-space: pre-wrap; word-break: break-word; max-height: 360px;
      overflow-y: auto; color: #c3c9d8; line-height: 1.5;
    }
    #aria-actions-panel .action-plan.empty { color: #6b7385; font-style: italic; padding: 12px; text-align: center; }
    #aria-actions-panel .action-controls {
      margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap;
    }
    #aria-actions-panel .action-btn {
      background: #1f6feb; color: #fff; border: 0;
      border-radius: 6px; padding: 6px 12px; font-size: 12px;
      cursor: pointer; font-weight: 500;
    }
    #aria-actions-panel .action-btn:hover { background: #2384ff; }
    #aria-actions-panel .action-btn.secondary {
      background: transparent; color: #9aa3b8; border: 1px solid #232438;
    }
    #aria-actions-panel .action-btn.secondary:hover {
      background: #14141f; color: #fff;
    }
    #aria-actions-panel .action-btn.danger {
      background: transparent; color: #ef4444; border: 1px solid #3a1d1d;
    }
    #aria-actions-panel .action-btn.danger:hover { background: #1a0d0d; }
    #aria-actions-panel .outcome-form {
      margin-top: 10px; display: none; flex-direction: column; gap: 8px;
    }
    #aria-actions-panel .outcome-form.open { display: flex; }
    #aria-actions-panel .outcome-form textarea {
      background: #14141f; border: 1px solid #232438; color: #e6e8ef;
      border-radius: 6px; padding: 8px 10px; font-size: 12px;
      font-family: ui-monospace, monospace; min-height: 70px; resize: vertical;
    }
    #aria-actions-panel .outcome-form input {
      background: #14141f; border: 1px solid #232438; color: #e6e8ef;
      border-radius: 6px; padding: 6px 10px; font-size: 12px; max-width: 200px;
      font-family: ui-monospace, monospace;
    }
    #aria-actions-panel .outcome-row {
      display: flex; gap: 8px; align-items: center;
    }
    #aria-actions-panel .outcome-row label { font-size: 11px; color: #9aa3b8; }
    #aria-actions-panel .actions-empty {
      color: #6b7385; font-size: 13px; text-align: center; padding: 40px 12px;
    }
  `;
  document.head.appendChild(style);
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatWhen(ts: number): string {
  const elapsed = Date.now() / 1000 - ts;
  if (elapsed < 60) return "just now";
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m ago`;
  if (elapsed < 86400) return `${Math.floor(elapsed / 3600)}h ago`;
  const days = Math.floor(elapsed / 86400);
  return days === 1 ? "yesterday" : `${days}d ago`;
}

function createPanel(): HTMLDivElement {
  const root = document.createElement("div");
  root.id = "aria-actions-panel";
  root.innerHTML = `
    <div class="actions-modal" role="dialog" aria-modal="true" aria-label="Actions">
      <div class="actions-header">
        <div>
          <h2>Actions Aria is tracking</h2>
          <div class="sub">Phone calls and emails she's drafted for you. v1: she writes the script, you place the call or send the email. Outbound automation lands next.</div>
        </div>
        <button class="actions-close" aria-label="Close">&times;</button>
      </div>
      <div class="actions-body">
        <div id="actions-list"></div>
        <div id="actions-empty" class="actions-empty" style="display:none">
          No actions yet. Say something like "help me negotiate my AT&amp;T bill" or "I need a refund email to Marriott" and Aria will draft it here.
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(root);
  const closeBtn = root.querySelector(".actions-close") as HTMLButtonElement;
  closeBtn.addEventListener("click", () => closeActionsPanel());
  root.addEventListener("click", (e) => {
    if (e.target === root) closeActionsPanel();
  });
  return root;
}

function renderCard(a: ActionRecord, root: HTMLDivElement): HTMLDivElement {
  const card = document.createElement("div");
  card.className = `action-card ${a.status}`;
  card.dataset.id = String(a.id);

  const planHtml = a.plan
    ? `<div class="action-plan">${escapeHtml(a.plan)}</div>`
    : `<div class="action-plan empty">Aria is still drafting this — refresh in a moment.</div>`;

  const vendor = a.vendor ? escapeHtml(a.vendor) : "(unnamed)";
  // `phone` field is reused as `recipient` for email_draft actions.
  const contactLabel = a.kind === "email_draft" ? "to" : "tel";
  const contactStr = a.phone
    ? ` · <span style="font-family:ui-monospace,monospace">${contactLabel} ${escapeHtml(a.phone)}</span>`
    : "";
  const kindLabel = a.kind === "email_draft" ? "email draft" : "call draft";

  const outcomeHtml = a.status === "completed" && (a.outcome_notes || a.outcome_value_cents)
    ? `<div class="action-meta" style="margin-top:8px;color:#7eda9b">
         Outcome: ${escapeHtml(a.outcome_notes || "(no notes)")}${a.outcome_value_cents ? ` · $${(a.outcome_value_cents / 100).toFixed(2)} recovered` : ""}
       </div>`
    : "";

  card.innerHTML = `
    <div class="action-header">
      <div class="action-title">
        <span class="action-vendor">${vendor}</span> — <span class="action-goal">${escapeHtml(a.goal)}</span>
      </div>
      <span class="status-badge ${a.status}">${a.status.replace("_", " ")}</span>
    </div>
    <div class="action-meta">#${a.id} · ${kindLabel} · ${formatWhen(a.created_at)}${contactStr}</div>
    ${planHtml}
    ${outcomeHtml}
    <div class="action-controls"></div>
    <form class="outcome-form">
      <textarea name="outcome-notes" placeholder="What happened on the call? Who did you speak to, what did they offer, what's the next step?"></textarea>
      <div class="outcome-row">
        <label>Dollar amount recovered/saved (optional): $</label>
        <input type="number" step="0.01" min="0" name="outcome-value" placeholder="0.00" />
      </div>
      <div class="outcome-row">
        <button type="button" class="action-btn" data-act="save-outcome">Save outcome & complete</button>
        <button type="button" class="action-btn secondary" data-act="cancel-outcome">Cancel</button>
      </div>
    </form>
  `;

  const controls = card.querySelector(".action-controls") as HTMLDivElement;
  const outcomeForm = card.querySelector(".outcome-form") as HTMLFormElement;

  if (a.status === "drafted") {
    const startBtn = mkBtn("Mark in progress", "");
    startBtn.addEventListener("click", async () => {
      await updateStatus(a.id, "in_progress");
      await refresh(root);
    });
    controls.appendChild(startBtn);
  }
  if (a.status === "drafted" || a.status === "in_progress") {
    const completeBtn = mkBtn("Record outcome", "");
    completeBtn.addEventListener("click", () => {
      outcomeForm.classList.add("open");
    });
    controls.appendChild(completeBtn);
    const abandonBtn = mkBtn("Abandon", "secondary");
    abandonBtn.addEventListener("click", async () => {
      if (!confirm("Mark this as abandoned? You can still delete it later.")) return;
      await updateStatus(a.id, "abandoned");
      await refresh(root);
    });
    controls.appendChild(abandonBtn);
  }
  const deleteBtn = mkBtn("Delete", "danger");
  deleteBtn.addEventListener("click", async () => {
    if (!confirm(`Delete action #${a.id}? This can't be undone.`)) return;
    await deleteAction(a.id);
    await refresh(root);
  });
  controls.appendChild(deleteBtn);

  outcomeForm.querySelector("[data-act=cancel-outcome]")?.addEventListener("click", () => {
    outcomeForm.classList.remove("open");
  });
  outcomeForm.querySelector("[data-act=save-outcome]")?.addEventListener("click", async () => {
    const notes = (outcomeForm.querySelector("[name=outcome-notes]") as HTMLTextAreaElement).value.trim();
    const valStr = (outcomeForm.querySelector("[name=outcome-value]") as HTMLInputElement).value.trim();
    let cents: number | null = null;
    if (valStr) {
      const v = parseFloat(valStr);
      if (!isNaN(v) && v >= 0) cents = Math.round(v * 100);
    }
    await saveOutcome(a.id, notes, cents);
    await refresh(root);
  });

  return card;
}

function mkBtn(text: string, variant: string): HTMLButtonElement {
  const b = document.createElement("button");
  b.className = `action-btn${variant ? " " + variant : ""}`;
  b.textContent = text;
  b.type = "button";
  return b;
}

async function refresh(root: HTMLDivElement) {
  const listEl = root.querySelector("#actions-list") as HTMLDivElement;
  const emptyEl = root.querySelector("#actions-empty") as HTMLDivElement;
  let actions: ActionRecord[] = [];
  try {
    const r = await fetch("/api/actions", withAuthHeaders());
    if (r.ok) {
      const payload = await r.json();
      actions = payload.actions || [];
    }
  } catch (_err) { /* silent */ }

  listEl.innerHTML = "";
  if (actions.length === 0) {
    emptyEl.style.display = "block";
    return;
  }
  emptyEl.style.display = "none";
  for (const a of actions) {
    listEl.appendChild(renderCard(a, root));
  }
}

async function updateStatus(id: number, status: ActionRecord["status"]) {
  await fetch(`/api/actions/${id}/status`, withAuthHeaders({
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  }));
}

async function saveOutcome(id: number, notes: string, cents: number | null) {
  await fetch(`/api/actions/${id}/outcome`, withAuthHeaders({
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      outcome_notes: notes,
      outcome_value_cents: cents,
      mark_completed: true,
    }),
  }));
}

async function deleteAction(id: number) {
  await fetch(`/api/actions/${id}`, withAuthHeaders({ method: "DELETE" }));
}

export async function openActionsPanel() {
  if (isOpen) return;
  injectStyles();
  if (!panelEl) panelEl = createPanel();
  isOpen = true;
  panelEl.style.display = "flex";
  requestAnimationFrame(() => panelEl!.classList.add("open"));
  await refresh(panelEl);
  // Poll for plan completion (Aria drafts the plan in the background after
  // dispatching [ACTION:CALL_DRAFT]; the row exists immediately but `plan`
  // fills in seconds later).
  pollTimer = window.setInterval(() => {
    if (isOpen && panelEl) refresh(panelEl);
  }, 4000);
}

export function closeActionsPanel() {
  if (!panelEl || !isOpen) return;
  isOpen = false;
  panelEl.classList.remove("open");
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  setTimeout(() => {
    if (panelEl) panelEl.style.display = "none";
  }, 220);
}

export function isActionsPanelOpen(): boolean {
  return isOpen;
}
