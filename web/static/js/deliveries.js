// "My downloads" (server mode): the visitor's own ready files / ZIPs — Save, and "Keep longer" (once).
// Lives at the top of the Library panel; the job cards on Downloads carry the same two buttons.

import { api } from "./api.js";
import { deliverySoon, leftText } from "./queue-logic.js";
import { toast, withBusy } from "./ui.js";
import { $, esc, fmtBytes } from "./util.js";

let rows = [];
const isServer = () => document.body.classList.contains("server-mode");

/** Fetch the list and repaint. Server mode only (the card is hidden otherwise); a failed refresh keeps what is shown. */
export async function loadDeliveries() {
  if (!isServer()) return;
  try { rows = await api.deliveries(); } catch { return; }
  render();
}

function rowHTML(d) {
  const soon = deliverySoon(d.expires_at);
  const icon = d.kind === "zip" ? "file-earmark-zip" : "file-earmark-music";
  return `<div class="my-dl-row" data-job="${esc(d.job_id)}">
    <i class="bi bi-${icon} grad-text"></i>
    <span class="my-dl-title" title="${esc(d.name)}">${esc(d.title || d.name)}</span>
    <span class="my-dl-meta mono">${fmtBytes(d.size)}</span>
    <span class="my-dl-meta exp${soon ? " soon" : ""}">${esc(leftText(d.expires_at))}</span>
    <button type="button" class="btn-grad${soon ? " soon" : ""}" data-save><i class="bi bi-download"></i>${d.kind === "zip" ? "Save ZIP" : "Save"}</button>
    ${d.extended ? "" : `<button type="button" class="btn-ghost sm" data-extend title="Keep it on the server a while longer (once)"><i class="bi bi-hourglass-split"></i>Keep longer</button>`}
  </div>`;
}

function render() {
  const box = $("#my-dl");
  if (!box) return;
  const now = Date.now() / 1000;
  rows = rows.filter((d) => d.expires_at > now);  // expired since the last fetch: the sweep deletes it
  $("#my-dl-list").innerHTML = rows.map(rowHTML).join("");
  $("#my-dl-empty").hidden = rows.length > 0;
}

async function onClick(e) {
  const row = e.target.closest(".my-dl-row");
  const btn = e.target.closest("button");
  if (!row || !btn) return;
  const id = row.dataset.job;
  if (btn.hasAttribute("data-save")) {
    const a = document.createElement("a");
    a.href = api.jobFileUrl(id); a.download = "";
    document.body.appendChild(a); a.click(); a.remove();
  } else if (btn.hasAttribute("data-extend")) {
    try {
      await withBusy(btn, async () => {
        const v = await api.extendDelivery(id);
        const d = rows.find((r) => r.job_id === id);
        if (d) Object.assign(d, { expires_at: v.expires_at, extended: true });
        render();
        window.dispatchEvent(new CustomEvent("deliveries-changed", { detail: { source: "list" } }));
        toast(leftText(v.expires_at).replace("available for", "Now available for"), "ok", { title: "Kept longer", ms: 3200 });
      });
    } catch (err) {
      toast(err.detail || err.message, "err");
      loadDeliveries();  // it may have expired or been extended elsewhere
    }
  }
}

export function initDeliveries() {
  const box = $("#my-dl");
  if (!box) return;
  box.addEventListener("click", onClick);
  $("#my-dl-refresh").addEventListener("click", (e) => withBusy(e.currentTarget, loadDeliveries));
  window.addEventListener("deliveries-changed", (e) => { if (e.detail?.source !== "list") loadDeliveries(); });
  setInterval(() => { if (isServer() && !$("#panel-library").hidden) render(); }, 30000);  // countdown text + urgency
}
