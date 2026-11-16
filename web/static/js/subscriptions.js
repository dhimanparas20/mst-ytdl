// Subscriptions: follow a channel / playlist; the server polls it every 15 minutes and downloads new uploads.
//
// Self-contained: this module injects its own header button, modal and stylesheet (nothing in
// index.html / app.css), so it can be moved into the page markup later without touching the logic.
// Other modules subscribe through a "subscribe-add" window event ({ url, title }) — the composer's
// playlist preview uses it — so they never import this one.
//
// The options of a subscription are a snapshot of the composer's CURRENT format / quality / switches.

import { api } from "./api.js";
import { currentOptions, formatLabel } from "./composer.js";
import { confirmDialog, toast, withBusy } from "./ui.js";
import { $, esc, fmtDate } from "./util.js";

const REFRESH_MS = 60_000;
let cfg = {};
let subs = [];
let timer = null;

/** "just now" / "5 min ago" / "2 h ago" / a date. */
function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return fmtDate(ts);
}

function ensureMarkup() {
  if ($("#subs-modal")) return;
  if (!document.querySelector('link[href$="subscriptions.css"]')) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "/static/css/subscriptions.css";
    document.head.appendChild(link);
  }
  const btn = document.createElement("button");
  btn.id = "open-subs";
  btn.type = "button";
  btn.className = "btn-icon";
  btn.setAttribute("aria-label", "Subscriptions");
  btn.setAttribute("data-bs-toggle", "tooltip");
  btn.title = "Subscriptions — follow channels & playlists";
  btn.innerHTML = '<i class="bi bi-bell-fill"></i>';
  const anchor = $("#open-settings") || $("#open-cart");
  if (anchor) anchor.before(btn); else document.body.appendChild(btn);

  document.body.insertAdjacentHTML("beforeend", `
  <div class="modal fade" id="subs-modal" tabindex="-1" aria-hidden="true">
    <div class="modal-dialog modal-dialog-centered modal-lg modal-dialog-scrollable">
      <div class="modal-content">
        <div class="modal-header"><h5 class="modal-title"><i class="bi bi-bell-fill me-2 grad-text"></i>Subscriptions <span class="faint" id="subs-head-count" style="font-size:14px"></span></h5><button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button></div>
        <div class="modal-body">
          <form id="subs-form" autocomplete="off">
            <div class="field-label">Follow a channel or playlist</div>
            <div class="flex gap-2">
              <input id="subs-url" class="form-control" type="text" inputmode="url" placeholder="https://www.youtube.com/@channel  or  a playlist link" style="padding:8px 12px">
              <button class="btn-grad" id="subs-add" type="submit" style="padding:8px 16px;white-space:nowrap"><i class="bi bi-bell-fill"></i>Subscribe</button>
            </div>
            <div class="opt mt-2" style="padding-left:2px"><div><div class="opt-t" style="font-size:13.5px">Skip Shorts</div><div class="opt-d">Ignore videos of 60 seconds or less</div></div><div class="form-check form-switch m-0"><input id="subs-shorts" class="form-check-input" type="checkbox" checked></div></div>
            <div class="faint mt-1" style="font-size:12.5px">New uploads are downloaded as <b id="subs-format">MP3</b> (your current choice on the New download tab). Videos that exist now are <b>not</b> downloaded — only future uploads. Checked every 15 minutes.</div>
          </form>
          <ul class="subs-list mt-3" id="subs-list"></ul>
          <div class="cart-empty" id="subs-empty"><i class="bi bi-bell"></i>No subscriptions yet.<div class="faint" style="font-size:12.5px">Paste a channel or playlist link above, or press <b>Subscribe</b> on a playlist preview.</div></div>
        </div>
        <div class="modal-footer"><span class="faint me-auto" id="subs-limit" style="font-size:12.5px"></span><button class="btn-ghost" data-bs-dismiss="modal">Close</button></div>
      </div>
    </div>
  </div>`);
  window.bootstrap?.Tooltip?.getOrCreateInstance(btn);
}

function row(s) {
  const kind = s.kind === "channel" ? "Channel" : "Playlist";
  const status = s.enabled ? "" : ' · <b>paused</b>';
  const meta = `${kind}${status} · checked ${ago(s.last_check)}${s.downloaded ? ` · ${s.downloaded} downloaded` : ""}`;
  const thumb = s.thumbnail ? `<img alt="" loading="lazy" src="${esc(s.thumbnail)}" onerror="this.style.visibility='hidden'">` : '<img alt="" style="visibility:hidden">';
  return `
    <li data-id="${esc(s.sub_id)}" class="${s.enabled ? "" : "paused"}">
      ${thumb}
      <div class="s-main">
        <div class="s-title"><span class="truncate-1" title="${esc(s.title)}">${esc(s.title)}</span></div>
        <div class="s-sub truncate-1" title="${esc(s.url)}">${meta}</div>
        ${s.last_error ? `<div class="s-err truncate-1" title="${esc(s.last_error)}"><i class="bi bi-exclamation-triangle-fill"></i> ${esc(s.last_error)}</div>` : ""}
      </div>
      <div class="s-actions">
        <button class="btn-icon sm" data-act="check" aria-label="Check now" title="Check now"><i class="bi bi-arrow-repeat"></i></button>
        <button class="btn-icon sm" data-act="${s.enabled ? "pause" : "resume"}" aria-label="${s.enabled ? "Pause" : "Resume"}" title="${s.enabled ? "Pause" : "Resume"}"><i class="bi bi-${s.enabled ? "pause-fill" : "play-fill"}"></i></button>
        <button class="btn-icon sm bad" data-act="delete" aria-label="Unsubscribe" title="Unsubscribe"><i class="bi bi-trash3"></i></button>
      </div>
    </li>`;
}

function render() {
  const n = subs.length;
  $("#subs-list").innerHTML = subs.map(row).join("");
  $("#subs-empty").hidden = n > 0;
  $("#subs-head-count").textContent = n ? `· ${n}` : "";
  $("#subs-limit").textContent = cfg.max_subscriptions ? `${n} of ${cfg.max_subscriptions}` : "";
  $("#subs-format").textContent = formatLabel();
  $("#open-subs").classList.toggle("has-subs", n > 0);
}

async function refresh() {
  try { subs = await api.subscriptions(); render(); } catch (e) { toast(e.detail || e.message, "err", { title: "Could not load subscriptions" }); }
}

/** Subscribe with the composer's current options. Resolves to true on success. */
async function subscribe(url, { title = "" } = {}) {
  const body = { ...currentOptions(), url: url.trim(), skip_shorts: $("#subs-shorts").checked };
  try {
    const s = await api.subscribe(body);
    toast(`New uploads of “${s.title || title}” will download automatically.`, "ok", { title: "Subscribed", ms: 4200 });
    await refresh();
    return true;
  } catch (e) {
    toast(e.detail || e.message, "err", { title: "Could not subscribe" });
    return false;
  }
}

function replace(updated) {
  subs = subs.map((x) => (x.sub_id === updated.sub_id ? updated : x));
  render();
}

async function onAction(e) {
  const btn = e.target.closest("[data-act]");
  const li = e.target.closest("li[data-id]");
  if (!btn || !li) return;
  const id = li.dataset.id;
  const sub = subs.find((x) => x.sub_id === id);
  try {
    switch (btn.dataset.act) {
      case "pause": replace(await api.subAction(id, "pause")); break;
      case "resume": replace(await api.subAction(id, "resume")); break;
      case "check": {
        btn.classList.add("spin");
        const r = await withBusy(btn, () => api.subAction(id, "check"));
        replace(r.subscription);
        if (r.error) toast(r.error, "warn", { title: "Check finished with a problem" });
        else toast(r.new ? `${r.new} new video${r.new === 1 ? "" : "s"} queued.` : "Nothing new.", r.new ? "ok" : "info", { title: sub?.title || "Checked", ms: 3000 });
        if (r.new) window.dispatchEvent(new CustomEvent("go-queue"));
        break;
      }
      case "delete":
        if (!(await confirmDialog({ title: "Unsubscribe?", body: `“${sub?.title || "This subscription"}” will no longer be checked. Files already downloaded stay.`, confirmText: "Unsubscribe", danger: true }))) return;
        await api.subDelete(id);
        subs = subs.filter((x) => x.sub_id !== id);
        render();
        break;
    }
  } catch (err) {
    toast(err.detail || err.message, "err");
    refresh();
  }
}

export function initSubscriptions(config) {
  cfg = config || {};
  ensureMarkup();
  const modal = $("#subs-modal");
  $("#open-subs").addEventListener("click", () => bootstrap.Modal.getOrCreateInstance(modal).show());
  modal.addEventListener("show.bs.modal", () => { render(); refresh(); clearInterval(timer); timer = setInterval(refresh, REFRESH_MS); });
  modal.addEventListener("hidden.bs.modal", () => { clearInterval(timer); timer = null; });
  $("#subs-list").addEventListener("click", onAction);
  $("#subs-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#subs-url");
    if (!input.value.trim()) return;
    if (await withBusy($("#subs-add"), () => subscribe(input.value))) input.value = "";
  });
  window.addEventListener("subscribe-add", (e) => { if (e.detail?.url) subscribe(e.detail.url, e.detail); });
  render();
  refresh();
}
