// "Your downloads" (server mode): the visitor's own ready files / ZIPs — Play, Save, Keep longer.
// This IS the visitor's library on a public server; the folder browser below is only shared host files.

import { api } from "./api.js";
import { deliverySoon, leftText } from "./queue-logic.js";
import { fileTrack } from "./player.js";
import { toast, withBusy } from "./ui.js";
import { $, esc, fmtBytes } from "./util.js";

let rows = [];
const isServer = () => document.body.classList.contains("server-mode");
const AUDIO_EXT = /\.(mp3|m4a|opus|flac|wav|ogg|aac)$/i;
const MEDIA_EXT = /\.(mp3|m4a|opus|flac|wav|ogg|aac|mp4|mkv|webm|mov)$/i;

/** Fetch the list and repaint. Server mode only (the card is hidden otherwise); a failed refresh keeps what is shown. */
export async function loadDeliveries() {
  if (!isServer()) return;
  try { rows = await api.deliveries(); } catch { return; }
  render();
}

function canPlay(d) {
  return d.kind === "file" && MEDIA_EXT.test(d.name || "");
}

function rowHTML(d) {
  const soon = deliverySoon(d.expires_at);
  const icon = d.kind === "zip" ? "file-earmark-zip" : AUDIO_EXT.test(d.name || "") ? "music-note-beamed" : "film";
  const play = canPlay(d)
    ? `<button type="button" class="btn-ghost sm" data-play title="Play"><i class="bi bi-play-fill"></i>Play</button>`
    : "";
  return `<div class="my-dl-row${soon ? " soon" : ""}" data-job="${esc(d.job_id)}" data-name="${esc(d.name)}" data-title="${esc(d.title || d.name)}" data-kind="${esc(d.kind)}">
    <div class="my-dl-ico"><i class="bi bi-${icon}"></i></div>
    <div class="my-dl-main">
      <div class="my-dl-title" title="${esc(d.title || d.name)}">${esc(d.title || d.name)}</div>
      <div class="my-dl-meta"><span class="mono">${fmtBytes(d.size)}</span>
        <span class="exp${soon ? " soon" : ""}">· ${esc(leftText(d.expires_at))}</span></div>
    </div>
    <div class="my-dl-actions">
      ${play}
      <button type="button" class="btn-grad${soon ? " soon" : ""}" data-save><i class="bi bi-download"></i>${d.kind === "zip" ? "Save ZIP" : "Save to device"}</button>
      ${d.extended ? "" : `<button type="button" class="btn-ghost sm" data-extend title="Keep it on the server a while longer (once)"><i class="bi bi-hourglass-split"></i>Keep longer</button>`}
    </div>
  </div>`;
}

function render() {
  const box = $("#my-dl");
  if (!box) return;
  const now = Date.now() / 1000;
  rows = rows.filter((d) => d.expires_at > now);  // expired since the last fetch: the sweep deletes it
  $("#my-dl-list").innerHTML = rows.map(rowHTML).join("");
  $("#my-dl-empty").hidden = rows.length > 0;
  box.classList.toggle("has-rows", rows.length > 0);
  const count = $("#my-dl-count");
  if (count) {
    count.hidden = rows.length === 0;
    count.textContent = `${rows.length} ready`;
  }
  const sub = $("#my-dl-sub");
  if (sub) {
    sub.textContent = rows.length
      ? "Press Save to keep a copy on your phone/PC — these are not stored in the shared folders below"
      : "Finish a download on the Downloads tab — it shows up here (not inside Normal Albums / English / … below)";
  }
}

function playDelivery(d) {
  const url = api.jobFileUrl(d.job_id);
  const track = fileTrack({
    path: d.name || "download",
    title: d.title || d.name,
    artist: "Your downloads",
    url,
    thumb: null,
  });
  track.key = `delivery:${d.job_id}`;
  window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: [track], start: 0 } }));
}

async function onClick(e) {
  const row = e.target.closest(".my-dl-row");
  const btn = e.target.closest("button");
  if (!row || !btn) return;
  const id = row.dataset.job;
  const d = rows.find((r) => r.job_id === id) || {
    job_id: id, name: row.dataset.name, title: row.dataset.title, kind: row.dataset.kind,
  };
  if (btn.hasAttribute("data-play")) {
    playDelivery(d);
  } else if (btn.hasAttribute("data-save")) {
    const a = document.createElement("a");
    a.href = api.jobFileUrl(id); a.download = "";
    document.body.appendChild(a); a.click(); a.remove();
    toast("Saving to your device…", "ok", { ms: 2200 });
  } else if (btn.hasAttribute("data-extend")) {
    try {
      await withBusy(btn, async () => {
        const v = await api.extendDelivery(id);
        const cur = rows.find((r) => r.job_id === id);
        if (cur) Object.assign(cur, { expires_at: v.expires_at, extended: true });
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
