// Settings modal (runtime limits + system info) and the header health pill.

import { api } from "./api.js";
import { toast, withBusy } from "./ui.js";
import { $, esc, setText } from "./util.js";
import { showRuntime } from "./queue.js";

let cfg = null;

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
  if (!c.loaded) return { ok: false, text: "No cookies loaded", detail: "Add cookies if YouTube asks you to sign in." };
  if (c.source === "browser" && !c.count) return { ok: true, text: `Loaded from ${esc(c.browser)}`, detail: "Read from the browser profile (host only)." };
  const warn = c.warnings[0];
  const bits = [`${c.count} cookies`, c.login ? "signed in" : "not signed in"];
  if (c.expires_at) bits.push(`login expires ${when(c.expires_at)}`);
  return { ok: c.login && !warn, warn: !!warn || !c.login, text: "Cookies loaded", detail: esc(warn || bits.join(" · ")) };
}

function paintCookies(c) {
  const s = cookieSummary(c);
  const icon = s.ok ? "check-circle-fill" : s.warn ? "exclamation-triangle-fill" : "x-circle-fill";
  const col = s.ok ? "ok" : s.warn ? "warn" : "bad";
  const html = `<i class="bi bi-${icon}" style="color:var(--${col})"></i> ${s.text}`;
  $("#ck-status").innerHTML = html;
  $("#ck-detail").innerHTML = s.detail;
  $("#ck-modal-status").innerHTML = `${html} <span class="faint">— ${s.detail}</span>`;
  $("#ck-path").textContent = c.path || "cookies/cookies.txt";
  $("#ck-clear").hidden = c.source !== "file";
}

async function loadCookies() {
  try { paintCookies(await api.cookies()); } catch (e) { $("#ck-status").textContent = "Unavailable"; }
}

async function openCookies() {
  $("#ck-text").value = "";
  $("#ck-test-result").textContent = "";
  bootstrap.Modal.getInstance($("#settings-modal"))?.hide();   // one modal at a time
  bootstrap.Modal.getOrCreateInstance($("#cookies-modal")).show();
  loadCookies();
}

async function saveCookies(btn) {
  try {
    const c = await withBusy(btn, () => api.saveCookies($("#ck-text").value));
    paintCookies(c);
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
  try { paintTest(await withBusy(btn, () => api.testCookies())); }
  catch (e) { $("#ck-test-result").textContent = e.detail || e.message; }
}

async function clearCookies(btn) {
  try { paintCookies(await withBusy(btn, () => api.clearCookies())); toast("Cookies removed.", "ok", { ms: 2500 }); }
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
    await Promise.all([fillSystem(), loadCookies()]);
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
  $("#ck-open").addEventListener("click", openCookies);
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
  pill.className = `health ${cls}`;
  pill.querySelector(".h-text").textContent = text;
  const tip = h ? (h.detail.length ? h.detail.join(" • ") : `yt-dlp ${h.yt_dlp} · ffmpeg ✓ · worker ✓`) : "Cannot reach the API";
  pill.setAttribute("data-bs-original-title", tip);
  pill.setAttribute("title", tip);
  const inst = bootstrap.Tooltip.getInstance(pill);
  if (inst) inst.setContent({ ".tooltip-inner": tip });

  const host = $("#banner-host");
  const msgs = [];
  if (h && !h.redis) msgs.push(`Redis is not reachable. Start it: <code>docker compose up -d redis</code>`);
  else if (h && !h.worker) msgs.push(`No download worker is running, so jobs will wait in the queue. Start one: <code>uv run python main.py worker</code>`);
  if (h && h.redis && !h.ffmpeg) msgs.push(`ffmpeg was not found — conversions will fail. Set <code>DL_FFMPEG_PATH</code> or install ffmpeg.`);
  host.innerHTML = msgs.map((m) => `<div class="banner"><i class="bi bi-exclamation-triangle-fill"></i><div>${m}</div></div>`).join("");
}
