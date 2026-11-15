// Download cart: collect songs from search results, playlist previews and pasted links,
// then download them in one go as ONE job (one folder; one ZIP in server mode or on request).
//
// The cart lives in this browser (localStorage, synced across tabs). Other modules add to it
// with a "cart-add" event ({ entries: [{url, video_id, title, uploader, thumbnail, duration}] }),
// so they never import this module.

import { api } from "./api.js";
import { currentOptions, formatLabel } from "./composer.js";
import { ytTrack } from "./player.js";
import { confirmDialog, toast, withBusy } from "./ui.js";
import { $, esc, fmtDuration, store } from "./util.js";

const KEY = "mst.cart";
const MAX = 500;
let items = load();
let cfg = {};

function load() {
  const v = store(KEY);
  return Array.isArray(v) ? v.filter((e) => e && typeof e.url === "string") : [];
}

const keyOf = (e) => e.video_id || e.url;
export const cartHas = (e) => items.some((x) => keyOf(x) === keyOf(e));
export const cartSize = () => items.length;

function changed({ pop = false } = {}) {
  store(KEY, items);
  paintBadge(pop);
  if ($("#cart-modal").classList.contains("show")) render();
  window.dispatchEvent(new CustomEvent("cart-change", { detail: { size: items.length } }));
}

/** Add entries (duplicates ignored); returns how many were new. */
export function addToCart(entries) {
  let added = 0;
  for (const e of entries || []) {
    if (!e?.url || cartHas(e) || items.length >= MAX) continue;
    items.push({
      url: e.url, video_id: e.video_id || null, title: e.title || "", uploader: e.uploader || null,
      thumbnail: e.thumbnail || null, duration: e.duration || null,
    });
    added++;
  }
  if (added) {
    changed({ pop: true });
    toast(added === 1 ? `Added “${entries.find((x) => x.title)?.title || "1 track"}”` : `${added} tracks added`, "ok", { title: `Cart · ${items.length}`, ms: 2200 });
  } else if ((entries || []).length) toast(items.length >= MAX ? `The cart holds at most ${MAX} tracks.` : "Already in your cart.", "info", { ms: 2000 });
  return added;
}

function paintBadge(pop) {
  const n = items.length;
  const el = $("#cart-count");
  el.textContent = n > 99 ? "99+" : String(n);
  el.classList.toggle("show", n > 0);
  if (pop) { const b = $("#open-cart"); b.classList.remove("pop"); void b.offsetWidth; b.classList.add("pop"); }
}

function render() {
  const list = $("#cart-list");
  const n = items.length;
  $("#cart-empty").hidden = n > 0;
  $("#cart-form").hidden = n === 0;
  $("#cart-clear").hidden = n === 0;
  $("#cart-play").hidden = n === 0;
  $("#cart-go").disabled = n === 0;
  $("#cart-head-count").textContent = n ? `· ${n} track${n === 1 ? "" : "s"}` : "";
  $("#cart-go-label").textContent = cfg.server_mode || $("#cart-zip").checked ? `Download ${n} as ZIP` : `Download all ${n}`;
  $("#cart-format").textContent = formatLabel();
  $("#cart-ttl").textContent = String(cfg.delivery_ttl_minutes || 60);
  const name = $("#cart-name").value.trim() || "Cart …";
  $("#cart-folder-hint").textContent = `downloads/${name}/`;
  list.innerHTML = items.map((e, i) => `
    <li data-k="${esc(keyOf(e))}">
      <span class="c-idx mono">${i + 1}</span>
      <img alt="" loading="lazy" src="${esc(e.thumbnail || "")}" onerror="this.style.visibility='hidden'">
      <div style="min-width:0"><div class="c-title truncate-1" title="${esc(e.title || e.url)}">${esc(e.title || e.url)}</div><div class="c-sub truncate-1">${esc(e.uploader || "")}</div></div>
      <span class="c-dur mono">${e.duration ? fmtDuration(e.duration) : ""}</span>
      <button class="btn-icon sm bad" data-rm aria-label="Remove" title="Remove"><i class="bi bi-x-lg"></i></button>
    </li>`).join("");
}

function open() {
  render();
  bootstrap.Modal.getOrCreateInstance($("#cart-modal")).show();
}

async function checkout(btn) {
  if (!items.length) return;
  const limit = cfg.server_mode ? cfg.max_job_items : cfg.max_playlist_items;
  if (limit && items.length > limit) return toast(`One download takes at most ${limit} tracks — remove ${items.length - limit}.`, "warn");
  const body = {
    ...currentOptions(),
    entries: items.map(({ url, title, uploader, thumbnail, duration }) => ({ url, title, uploader, thumbnail, duration })),
    name: $("#cart-name").value.trim() || null,
    zip: !cfg.server_mode && $("#cart-zip").checked,
  };
  try {
    const res = await withBusy(btn, () => api.checkoutCart(body));
    res.rejected.forEach((r) => toast(`${r.url} — ${r.error}`, "warn", { title: "Skipped" }));
    items = [];
    $("#cart-name").value = "";
    changed();
    bootstrap.Modal.getInstance($("#cart-modal"))?.hide();
    toast(`${res.job.total_items} track${res.job.total_items === 1 ? "" : "s"} → “${res.job.title}”${res.job.deliver_zip ? " · the ZIP downloads when it is ready" : ""}`, "ok", { title: "Cart queued", ms: 4200 });
    window.dispatchEvent(new CustomEvent("go-queue"));
  } catch (e) { toast(e.detail || e.message, "err", { title: "Could not queue the cart" }); }
}

export function initCart(config) {
  cfg = config || {};
  paintBadge(false);
  $("#open-cart").addEventListener("click", open);
  $("#cart-list").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-k]");
    if (!li || !e.target.closest("[data-rm]")) return;
    items = items.filter((x) => keyOf(x) !== li.dataset.k);
    changed();
  });
  $("#cart-clear").addEventListener("click", async () => {
    if (!(await confirmDialog({ title: "Empty the cart?", body: `${items.length} track${items.length === 1 ? "" : "s"} will be removed from the cart.`, confirmText: "Empty", danger: true }))) return;
    items = []; changed();
  });
  $("#cart-play").addEventListener("click", () => {
    window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: items.map((e) => ytTrack({ ...e, video_id: e.video_id || (e.url.match(/[?&]v=([\w-]{11})/) || [])[1] })), start: 0 } }));
  });
  $("#cart-go").addEventListener("click", (e) => checkout(e.currentTarget));
  $("#cart-zip").addEventListener("change", render);
  $("#cart-name").addEventListener("input", () => { $("#cart-folder-hint").textContent = `downloads/${$("#cart-name").value.trim() || "Cart …"}/`; });
  $("#cart-modal").addEventListener("show.bs.modal", render);
  window.addEventListener("cart-add", (e) => addToCart(e.detail?.entries));
  window.addEventListener("storage", (e) => { if (e.key === KEY) { items = load(); paintBadge(false); if ($("#cart-modal").classList.contains("show")) render(); } });
}
