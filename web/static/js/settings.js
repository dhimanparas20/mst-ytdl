// Settings modal (runtime limits + system info) and the header health pill.

import { api } from "./api.js";
import { toast, withBusy } from "./ui.js";
import { $, cookieChipState, esc, setText } from "./util.js";
import { showRuntime } from "./queue.js";

let cfg = null;
let ckTarget = "server";  // which cookies the modal edits: "server" (admin / personal) or "mine" (server-mode visitor)
const CK = {
  server: { get: () => api.cookies(), save: (t) => api.saveCookies(t), clear: () => api.clearCookies(), test: () => api.testCookies() },
  mine: { get: () => api.myCookies(), save: (t) => api.saveMyCookies(t), clear: () => api.clearMyCookies(), test: () => api.testMyCookies() },
};

function shortPath(p, n = 46) {
  if (!p) return "—";
  return p.length > n ? "…" + p.slice(-(n - 1)) : p;
}

const row = (k, v, ok) =>
  `<div class="flex justify-between gap-3 py-1" style="border-bottom:1px solid var(--stroke)"><span class="muted">${k}</span><span class="mono text-end" style="word-break:break-all">${ok === undefined ? "" : `<i class="bi bi-${ok ? "check-circle-fill" : "x-circle-fill"}" style="color:var(--${ok ? "ok" : "bad"})"></i> `}${v}</span></div>`;

async function fillSystem() {
  const [h, c] = await Promise.all([api.health(), cfg ? Promise.resolve(cfg) : api.config()]);
  cfg = c;
  $("#sys-info").innerHTML = [
    row("Redis", h.redis ? "connected" : "unreachable", h.redis),
    row("Worker", h.worker ? "running" : "not seen", h.worker),
    row("ffmpeg", h.ffmpeg ? esc(shortPath(h.ffmpeg, 38)) : "missing", !!h.ffmpeg),
    row("JS runtime", h.js_runtime ? esc(h.js_runtime) : "none", !!h.js_runtime),
    row("yt-dlp", esc(h.yt_dlp)),
    row("Downloads", esc(shortPath(h.downloads_dir))),
    row("Timezone", esc(c.timezone)),
    row("Worker threads", String(c.workers)),
  ].join("");
}

// ── YouTube cookies ──
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "");

function cookieSummary(c) {
  const mine = c.scope === "visitor";
  if (!c.loaded) return { ok: false, text: mine ? "None — you browse anonymously" : "No cookies loaded", detail: mine ? "Add yours if YouTube asks you to sign in." : "Add cookies if YouTube asks you to sign in." };
  if (c.source === "browser" && !c.count) return { ok: true, text: `Loaded from ${esc(c.browser)}`, detail: "Read from the browser profile (host only)." };
  const warn = c.warnings[0];
  const bits = [`${c.count} cookies`, c.login ? "signed in" : "not signed in"];
  if (c.expires_at) bits.push(`login expires ${when(c.expires_at)}`);
  if (mine && c.deleted_at) bits.push(`deleted from the server ${when(c.deleted_at)}`);
  return { ok: c.login && !warn, warn: !!warn || !c.login, text: "Cookies loaded", detail: esc(warn || bits.join(" · ")) };
}

function paintCookies(c, target = ckTarget) {
  const s = cookieSummary(c);
  const icon = s.ok ? "check-circle-fill" : s.warn ? "exclamation-triangle-fill" : "x-circle-fill";
  const col = s.ok ? "ok" : s.warn ? "warn" : "bad";
  const html = `<i class="bi bi-${icon}" style="color:var(--${col})"></i> ${s.text}`;
  const row = target === "mine" ? "#myck" : "#ck";
  $(`${row}-status`).innerHTML = html;
  $(`${row}-detail`).innerHTML = s.detail;
  if (target !== ckTarget) return;  // the modal shows the other set
  $("#ck-modal-status").innerHTML = `${html} <span class="faint">— ${s.detail}</span>`;
  if ($("#ck-path")) $("#ck-path").textContent = c.path || "cookies/cookies.txt";
  $("#ck-clear").hidden = c.source !== "file";
}

/** Topbar chip: "Cookies · signed in / none / expiring". Server mode shows the visitor's own cookies, never the server's. */
async function refreshCookieChip() {
  const chip = $("#cookie-chip");
  if (!chip) return;
  let state = null;
  try {
    cfg = cfg || await api.config();
    state = cookieChipState(await CK[cfg.server_mode ? "mine" : "server"].get());
  } catch { /* status unavailable → hide the chip */ }
  chip.hidden = !state;
  if (!state) return;
  chip.className = `tag cookie-chip ${state.tone}`;
  setText($("#cookie-chip-text"), state.text);
  chip.setAttribute("aria-label", state.text);
  chip.setAttribute("data-bs-original-title", state.title);
  chip.setAttribute("title", state.title);
  bootstrap.Tooltip.getInstance(chip)?.setContent({ ".tooltip-inner": state.title });
}

async function loadCookies(target = ckTarget) {
  try { paintCookies(await CK[target].get(), target); }
  catch (e) { $(target === "mine" ? "#myck-status" : "#ck-status").textContent = e.status === 403 ? "Admin only" : "Unavailable"; }
}

async function openCookies(target = "server") {
  ckTarget = target;
  const mine = target === "mine";
  $("#ck-modal-title").textContent = mine ? "Your YouTube cookies" : "YouTube cookies";
  $("#ck-where").innerHTML = mine
    ? `They are kept on the server for ${esc(String(cfg?.visitor_cookie_hours || 24))} hours, used only for your own downloads and playback, and never shown again.`
    : `The values are stored on the server in <span class="mono" id="ck-path">cookies/cookies.txt</span> and are never shown again.`;
  $("#ck-text").value = "";
  $("#ck-test-result").textContent = "";
  bootstrap.Modal.getInstance($("#settings-modal"))?.hide();   // one modal at a time
  bootstrap.Modal.getOrCreateInstance($("#cookies-modal")).show();
  loadCookies();
}

async function saveCookies(btn) {
  try {
    const c = await withBusy(btn, () => CK[ckTarget].save($("#ck-text").value));
    paintCookies(c);
    window.dispatchEvent(new CustomEvent("cookies-changed"));
    $("#ck-text").value = "";
    toast(c.warnings.length ? c.warnings.join(" ") : "Cookies saved — used immediately.", c.login ? "ok" : "warn", { ms: 4500 });
    testCookies($("#ck-test"));   // validate against YouTube right away and show the verdict
  } catch (e) { toast(e.detail || e.message, "err"); }
}

function paintTest(r) {
  const el = $("#ck-test-result");
  el.innerHTML = `<i class="bi bi-${r.ok ? "check-circle-fill" : "x-circle-fill"}" style="color:var(--${r.ok ? "ok" : "bad"})"></i> ${esc(r.message)} <span class="faint">(${r.took_ms} ms)</span>`;
}

async function testCookies(btn) {
  $("#ck-test-result").textContent = "Checking with YouTube…";
  try { paintTest(await withBusy(btn, () => CK[ckTarget].test())); }
  catch (e) { $("#ck-test-result").textContent = e.detail || e.message; }
}

async function clearCookies(btn) {
  try { paintCookies(await withBusy(btn, () => CK[ckTarget].clear())); window.dispatchEvent(new CustomEvent("cookies-changed")); toast(ckTarget === "mine" ? "Your cookies were deleted from the server." : "Cookies removed.", "ok", { ms: 2500 }); }
  catch (e) { toast(e.detail || e.message, "err"); }
}

// ── default download folder ──
async function toggleFolderPicker() {
  const box = $("#s-folder-pick");
  if (!box.hidden) { box.hidden = true; return; }
  let list = [];
  try { list = await api.libFolders(); } catch { /* empty */ }
  const item = (value, label, icon) => `<button type="button" class="btn-ghost sm w-100 text-start" data-f="${esc(value)}" style="justify-content:flex-start"><i class="bi bi-${icon}"></i><span class="truncate-1">${esc(label)}</span></button>`;
  box.innerHTML = item("", "downloads/ (date & playlist folders)", "house") + list.map((f) => item(f, f, "folder2")).join("");
  box.hidden = false;
}

async function openSettings() {
  const modal = bootstrap.Modal.getOrCreateInstance($("#settings-modal"));
  modal.show();
  try {
    const [rt, c] = await Promise.all([api.runtime(), cfg ? cfg : api.config()]);
    cfg = c;
    const slider = $("#s-par");
    slider.max = c.max_parallel_cap;
    slider.value = rt.max_parallel;
    setText($("#s-par-val"), String(rt.max_parallel));
    setText($("#s-workers"), String(c.workers));
    $("#s-folder").value = rt.default_folder || "";
    $("#s-folder-pick").hidden = true;
    setText($("#s-root"), c.downloads_dir ? c.downloads_dir.replace(/[\\/]+$/, "") + "/" : "downloads/");
    if (rt.speed_limit) {
      const mb = rt.speed_limit >= 1048576 && rt.speed_limit % 1048576 === 0;
      $("#s-speed-unit").value = mb ? "1048576" : "1024";
      $("#s-speed").value = Math.round(rt.speed_limit / (mb ? 1048576 : 1024));
    } else { $("#s-speed").value = ""; }
    $("#myck-hours").textContent = String(c.visitor_cookie_hours || 24);
    const isServer = document.body.classList.contains("server-mode");
    await Promise.all([fillSystem(), loadCookies(isServer ? "mine" : "server")]);
  } catch (e) { toast(e.detail || e.message, "err"); }
}

async function save(btn) {
  try {
    const par = Number($("#s-par").value);
    const raw = Number($("#s-speed").value);
    const limit = raw > 0 ? Math.round(raw * Number($("#s-speed-unit").value)) : null;
    const rt = await withBusy(btn, () => api.setRuntime({ max_parallel: par, speed_limit: limit, default_folder: $("#s-folder").value.trim() || null }));
    showRuntime(rt);
    window.dispatchEvent(new CustomEvent("folders-changed"));
    toast("Settings applied — new downloads use them immediately.", "ok", { ms: 3000 });
    bootstrap.Modal.getInstance($("#settings-modal"))?.hide();
  } catch (e) { toast(e.detail || e.message, "err"); }
}

export function initSettings() {
  $("#open-settings").addEventListener("click", openSettings);
  $("#health-pill").addEventListener("click", openSettings);
  $("#s-par").addEventListener("input", (e) => setText($("#s-par-val"), e.target.value));
  $("#s-save").addEventListener("click", (e) => save(e.currentTarget));
  $("#s-folder-browse").addEventListener("click", toggleFolderPicker);
  $("#s-folder-pick").addEventListener("click", (e) => {
    const b = e.target.closest("[data-f]");
    if (b) { $("#s-folder").value = b.dataset.f; $("#s-folder-pick").hidden = true; }
  });
  $("#ck-open").addEventListener("click", () => openCookies("server"));
  $("#myck-open").addEventListener("click", () => openCookies("mine"));
  // the config may not be loaded yet (it is fetched when the settings dialog first opens): the target depends on it
  window.addEventListener("cookies-changed", () => { loadCookies(); refreshCookieChip(); });
  $("#cookie-chip")?.addEventListener("click", () => window.dispatchEvent(new CustomEvent("open-cookies")));
  refreshCookieChip();  // loads the config (mode) first, so it also primes `cfg` for the dialog
  window.addEventListener("open-cookies", async () => {
    try { cfg = cfg || await api.config(); } catch { /* fall back to the personal-mode editor */ }
    openCookies(cfg?.server_mode ? "mine" : "server");
  });
  $("#ck-save").addEventListener("click", (e) => saveCookies(e.currentTarget));
  $("#ck-clear").addEventListener("click", (e) => clearCookies(e.currentTarget));
  $("#ck-test").addEventListener("click", (e) => testCookies(e.currentTarget));
}

/** Update the header pill + the "no worker" banner. */
export function renderHealth(h) {
  const pill = $("#health-pill");
  let cls = "ok", text = "All systems go";
  if (!h) { cls = "bad"; text = "Server offline"; }
  else if (!h.redis) { cls = "bad"; text = "Redis down"; }
  else if (!h.worker) { cls = "warn"; text = "No worker"; }
  else if (!h.ffmpeg) { cls = "warn"; text = "ffmpeg missing"; }
  else if (!h.js_runtime) { cls = "warn"; text = "No JS runtime"; }
  pill.className = `health ${cls}`;
  pill.querySelector(".h-text").textContent = text;
  const tip = h ? (h.detail.length ? h.detail.join(" • ") : `yt-dlp ${h.yt_dlp} · ffmpeg ✓ · worker ✓ · ${h.js_runtime || "no JS"}`) : "Cannot reach the API";
  pill.setAttribute("data-bs-original-title", tip);
  pill.setAttribute("title", tip);
  const inst = bootstrap.Tooltip.getInstance(pill);
  if (inst) inst.setContent({ ".tooltip-inner": tip });

  const host = $("#banner-host");
  const msgs = [];
  if (h && !h.redis) msgs.push(`Redis is not reachable. Start it: <code>docker compose up -d redis</code>`);
  else if (h && !h.worker) msgs.push(`No download worker is running, so jobs will wait in the queue. Start one: <code>uv run python main.py worker</code>`);
  if (h && h.redis && !h.ffmpeg) msgs.push(`ffmpeg was not found — conversions will fail. Set <code>DL_FFMPEG_PATH</code> or install ffmpeg.`);
  if (h && h.redis && !h.js_runtime) msgs.push(`No JS runtime (node/deno/bun) — YouTube formats may fail. Install Node 22+ or set <code>DL_JS_RUNTIME</code>.`);
  host.innerHTML = msgs.map((m) => `<div class="banner"><i class="bi bi-exclamation-triangle-fill"></i><div>${m}</div></div>`).join("");
}
