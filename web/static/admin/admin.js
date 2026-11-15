// mst-ytdl admin dashboard: sign-in, overview (KPIs + charts), activity, visitors & bans,
// file manager, live settings, server cookies, maintenance, logs.
// Talks only to /api/admin/* (plus /api/runtime and /api/cookies, which accept the admin session).

import { confirmDialog, promptDialog, toast, withBusy } from "/static/js/ui.js";
import { $, $$, esc, fmtBytes, fmtDate } from "/static/js/util.js";

// ───────────────────────── api ─────────────────────────

class ApiError extends Error { constructor(status, detail) { super(detail); this.status = status; this.detail = detail; } }

async function call(method, path, body) {
  let res;
  try {
    res = await fetch("/api" + path, { method, headers: body !== undefined ? { "Content-Type": "application/json" } : undefined, body: body !== undefined ? JSON.stringify(body) : undefined });
  } catch { throw new ApiError(0, "Cannot reach the server"); }
  if (res.status === 403 && path !== "/admin/login") { showLogin(); throw new ApiError(403, "Signed out"); }
  if (!res.ok) {
    let j = null; try { j = await res.json(); } catch { /* not JSON */ }
    const d = j?.detail;
    throw new ApiError(res.status, typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : res.statusText);
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}
const get = (p) => call("GET", p), post = (p, b) => call("POST", p, b ?? {}), put = (p, b) => call("PUT", p, b), del = (p) => call("DELETE", p);
const qs = (o) => "?" + new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
const err = (e) => { if (e.status !== 403) toast(e.detail || e.message, "err"); };
const ago = (ts) => { if (!ts) return "—"; const s = Date.now() / 1000 - ts; if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)} min ago`; if (s < 86400) return `${Math.floor(s / 3600)} h ago`; return fmtDate(ts); };
const n = (v) => Number(v || 0).toLocaleString();

// ───────────────────────── session + routing ─────────────────────────

const VIEWS = { overview: "Overview", activity: "Activity", visitors: "Visitors", files: "Files", settings: "Settings", cookies: "Cookies", maintenance: "Maintenance", logs: "Logs" };
const LOADERS = {};
let view = null, timer = null, session = null;

function showLogin(msg) {
  $("#shell").hidden = true;
  $("#login").hidden = false;
  clearInterval(timer);
  if (msg) $("#login-err").textContent = msg;
  setTimeout(() => ($("#login-user").value ? $("#login-pw") : $("#login-user")).focus(), 50);
}

function showShell() {
  $("#login").hidden = true;
  $("#shell").hidden = false;
  $("#who").textContent = session?.username ? `Sign out (${session.username})` : "Sign out";
  $("#logout").hidden = !session?.enabled;
  const srv = session?.server_mode;
  $("#side-mode").innerHTML = `<i class="bi bi-${srv ? "globe2" : "laptop"}"></i><span>${srv ? "Server mode" : "Local mode"}</span>`;
  route();
}

async function boot() {
  try { session = await get("/admin"); } catch { session = null; }
  if (!session) return showLogin("Cannot reach the server.");
  if (session.admin) return showShell();
  if (!session.enabled && session.server_mode) {
    $("#login-sub").innerHTML = "No admin password is set. Run <code>uv run python main.py hash-password</code> and put the line in <code>.env</code>.";
    $("#login-form").querySelectorAll("input, button").forEach((x) => { x.disabled = true; });
  }
  showLogin();
}

async function login(e) {
  e.preventDefault();
  $("#login-err").textContent = "";
  try {
    session = await withBusy($("#login-btn"), () => post("/admin/login", { username: $("#login-user").value.trim(), password: $("#login-pw").value }));
    $("#login-pw").value = "";
    showShell();
  } catch (ex) { $("#login-err").textContent = ex.detail || ex.message; $("#login-pw").select(); }
}

function route() {
  const v = VIEWS[location.hash.slice(1)] ? location.hash.slice(1) : "overview";
  view = v;
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === v));
  $$("section[data-panel]").forEach((s) => { s.hidden = s.dataset.panel !== v; });
  $("#view-title").textContent = VIEWS[v];
  closeMenu();
  clearInterval(timer);
  const L = LOADERS[v];
  if (!L) return;
  const run = () => L.load().catch(err);
  run();
  if (L.every) timer = setInterval(() => { if (!document.hidden && view === v) run(); }, L.every);
}

const openMenu = () => { $("#side").classList.add("open"); $("#scrim").hidden = false; };
const closeMenu = () => { $("#side").classList.remove("open"); $("#scrim").hidden = true; };

// ───────────────────────── overview ─────────────────────────

const charts = {};
Chart.defaults.color = "#9b97b8";
Chart.defaults.borderColor = "rgba(255,255,255,.08)";
Chart.defaults.font.family = "Inter, system-ui, sans-serif";
const C1 = "#8c7bf0", C2 = "#fb7185", C3 = "#34d399", C4 = "#fbbf24";

function chart(id, cfg) {
  if (charts[id]) {
    charts[id].data.labels = cfg.data.labels;
    cfg.data.datasets.forEach((d, i) => { if (charts[id].data.datasets[i]) charts[id].data.datasets[i].data = d.data; });
    charts[id].update("none");
    return;
  }
  charts[id] = new Chart($(id), { ...cfg, options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false }, plugins: { legend: { labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } } }, ...cfg.options } });
}

const kpi = (icon, label, value, sub = "", cls = "") => `<div class="kpi glass ${cls}"><div class="k-label"><i class="bi bi-${icon}"></i>${label}</div><div class="k-val">${value}</div><div class="k-sub">${sub}</div></div>`;

LOADERS.overview = {
  every: 5000,
  async load() {
    const o = await get("/admin/overview");
    const t = o.stats.today || {}, q = o.queue, st = o.storage, d = o.deliveries;
    $("#maint-pill").hidden = !o.maintenance;
    const freePct = st.disk_total ? (st.disk_free / st.disk_total) * 100 : 100;
    $("#kpis").innerHTML = [
      kpi("broadcast", "Online now", n(o.stats.online), "visitors in the last 5 min", "info"),
      kpi("cloud-arrow-down", "Downloading", n(q.active), `${n(q.queued)} queued · ${n(q.paused)} paused`, q.active ? "info" : ""),
      kpi("check2-circle", "Done today", n(t.completed), `${n(t.cache_hits)} from cache`, "ok"),
      kpi("exclamation-triangle", "Failed today", n(t.failed), `${n(t.cancelled)} cancelled`, t.failed ? "bad" : ""),
      kpi("hdd-network", "Data today", fmtBytes(t.bytes || 0), `${n(t.saves)} saved to devices`),
      kpi("people", "Visitors today", n(t.visitors), `${n(t.submitted)} downloads queued`),
      kpi("device-hdd", "Disk free", fmtBytes(st.disk_free), `${freePct.toFixed(0)} % of ${fmtBytes(st.disk_total)}`, freePct < 10 ? "bad" : freePct < 20 ? "warn" : ""),
      kpi("cpu", "Worker", o.health.worker ? "Running" : "Down", `${o.health.running_slots} of ${o.runtime.max_parallel} slots busy`, o.health.worker ? "ok" : "bad"),
    ].join("");

    const H = o.stats.hourly;
    chart("#ch-hours", { type: "bar", data: { labels: H.map((x) => x.hour), datasets: [
      { label: "Completed", data: H.map((x) => x.completed || 0), backgroundColor: C1, borderRadius: 6, stack: "s" },
      { label: "Failed", data: H.map((x) => x.failed || 0), backgroundColor: C2, borderRadius: 6, stack: "s" },
      { label: "MB", data: H.map((x) => +((x.bytes || 0) / 1048576).toFixed(1)), type: "line", borderColor: C3, backgroundColor: "rgba(52,211,153,.12)", fill: true, tension: .35, pointRadius: 0, yAxisID: "y2" },
    ] }, options: { scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } }, y2: { position: "right", beginAtZero: true, grid: { display: false } } } } });

    const D = o.stats.daily;
    chart("#ch-days", { type: "bar", data: { labels: D.map((x) => x.day.slice(5)), datasets: [
      { label: "Completed", data: D.map((x) => x.completed || 0), backgroundColor: C1, borderRadius: 6 },
      { label: "Failed", data: D.map((x) => x.failed || 0), backgroundColor: C2, borderRadius: 6 },
      { label: "Visitors", data: D.map((x) => x.visitors || 0), type: "line", borderColor: C4, tension: .35, pointRadius: 2, yAxisID: "y2" },
    ] }, options: { scales: { x: { grid: { display: false } }, y: { beginAtZero: true, ticks: { precision: 0 } }, y2: { position: "right", beginAtZero: true, grid: { display: false }, ticks: { precision: 0 } } } } });

    const F = Object.entries(o.stats.formats).sort((a, b) => b[1] - a[1]);
    chart("#ch-formats", { type: "doughnut", data: { labels: F.length ? F.map((f) => f[0].toUpperCase()) : ["none yet"], datasets: [{ data: F.length ? F.map((f) => f[1]) : [1], backgroundColor: F.length ? ["#8c7bf0", "#34d399", "#f0a6ca", "#fbbf24", "#60a5fa", "#fb7185", "#22d3ee", "#a3e635"] : ["rgba(255,255,255,.08)"], borderWidth: 0 }] }, options: { cutout: "68%", plugins: { legend: { position: "right" } } } });

    const use = [["Searches", t.searches], ["Previews", t.previews], ["Plays", t.plays], ["Lyrics", t.lyrics], ["Carts", t.carts], ["Saves", t.saves], ["Queued", t.submitted], ["Cache hits", t.cache_hits]];
    $("#today-usage").innerHTML = use.map(([k, v]) => `<div><span class="muted">${k}</span><b>${n(v)}</b></div>`).join("");

    const pct = (v) => (st.disk_total ? Math.max(0, (v / st.disk_total) * 100) : 0);
    $("#storage-box").innerHTML = `<div class="flex justify-between" style="justify-content:space-between"><b>${fmtBytes(st.disk_used)}</b><span class="muted">of ${fmtBytes(st.disk_total)}</span></div>
      <div class="st-bar"><i class="lib" style="width:${pct(st.bytes)}%"></i><i class="dlv" style="width:${pct(d.bytes)}%"></i><i class="other" style="width:${Math.max(0, pct(st.disk_used) - pct(st.bytes) - pct(d.bytes))}%"></i></div>
      <div class="adm-mini-stats"><div><span class="muted">Library</span><b>${fmtBytes(st.bytes)}</b></div><div><span class="muted">Files</span><b>${n(st.files)}</b></div>
      <div><span class="muted">Deliveries</span><b>${fmtBytes(d.bytes)}</b></div><div><span class="muted">Waiting jobs</span><b>${n(d.jobs)}</b></div></div>`;

    const rank = (list, key) => list.length ? list.map((x) => `<li><span title="${esc(x[key])}">${esc(x[key])}</span><b>${x.count}</b></li>`).join("") : `<li class="adm-dim" style="display:block">Nothing yet today.</li>`;
    $("#top-videos").innerHTML = rank(o.stats.top_videos, "title");
    $("#top-searches").innerHTML = rank(o.stats.top_searches, "query");
    $("#errors").innerHTML = o.stats.errors.length ? o.stats.errors.map((e) => `<li><b title="${esc(e.title)}">${esc(e.title)}</b><span>${esc(e.error)}</span><br><small>${ago(e.at)}</small></li>`).join("") : `<li class="adm-dim">No failures. 🎉</li>`;

    const L = o.limits;
    $("#limits").innerHTML = [
      ["Worker threads", L.workers], ["Max parallel", L.max_parallel_cap], ["Max speed", L.max_speed ? `${fmtBytes(L.max_speed)}/s` : "none"],
      ["Jobs / visitor", L.max_active_jobs], ["Tracks / job", L.max_job_items], ["Files kept", `${L.delivery_ttl_minutes} min`], ["Requests / IP / min", L.rate_limit_per_minute],
      ["Admin sessions", o.admin.sessions], ["Blocked IPs", o.admin.bans], ["Failed-login IPs", o.admin.login_failures],
    ].map(([k, v]) => `<span class="tag">${k}<b>${esc(String(v))}</b></span>`).join("");
  },
};

// ───────────────────────── activity ─────────────────────────

const jobSel = new Set();
let jobFilter = "";
const STATUS_CLS = { completed: "st-completed", failed: "st-failed", cancelled: "st-cancelled", partial: "st-partial", downloading: "st-downloading", processing: "st-processing", queued: "st-queued", paused: "st-paused", resolving: "st-resolving" };

LOADERS.activity = {
  every: 3000,
  async load() {
    const jobs = await get("/admin/jobs" + qs({ status: jobFilter, limit: 300 }));
    const body = $("#jobs-table tbody");
    for (const id of [...jobSel]) if (!jobs.some((j) => j.job_id === id)) jobSel.delete(id);
    body.innerHTML = jobs.map((j) => {
      const fmt = j.options.format.toUpperCase() + (j.options.quality !== "best" ? ` ${j.options.quality}${["mp3", "m4a"].includes(j.options.format) ? "k" : "p"}` : "");
      const pct = j.status === "completed" ? 100 : j.percent;
      return `<tr data-id="${esc(j.job_id)}" class="${jobSel.has(j.job_id) ? "sel" : ""}">
        <td><input type="checkbox" class="form-check-input" ${jobSel.has(j.job_id) ? "checked" : ""}></td>
        <td><div class="t-title"><img src="${esc(j.thumbnail || "")}" alt="" loading="lazy" onerror="this.style.visibility='hidden'"><div><b title="${esc(j.title)}">${esc(j.title)}</b><small>${j.kind}${j.total_items > 1 ? ` · ${j.total_items} tracks` : ""}${j.speed ? ` · ↓ ${fmtBytes(j.speed)}/s` : ""}</small></div></div></td>
        <td class="mono">${j.owner ? esc(j.owner.slice(0, 8)) : "<span class='faint'>local</span>"}</td>
        <td><span class="st-pill sm ${STATUS_CLS[j.status] || ""}"><i class="dot"></i><span class="pt">${j.status}</span></span></td>
        <td><div class="pbar thin s-${j.status} ${["downloading", "processing"].includes(j.status) ? "active" : ""}"><i style="width:${pct}%"></i></div><small class="mono faint">${pct.toFixed(0)} %</small></td>
        <td class="mono">${fmt}</td>
        <td class="mono">${j.total_bytes ? fmtBytes(j.total_bytes) : j.downloaded_bytes ? fmtBytes(j.downloaded_bytes) : "—"}</td>
        <td class="mono">${ago(j.created_at)}</td></tr>`;
    }).join("");
    $("#jobs-empty").hidden = jobs.length > 0;
    $("#job-sel").textContent = `${jobSel.size} selected`;
    $("#jobs-all").checked = jobs.length > 0 && jobs.every((j) => jobSel.has(j.job_id));
  },
};

async function jobBulk(action, btn) {
  if (!jobSel.size) return toast("Select some downloads first.", "info");
  if (["cancel", "remove"].includes(action) && !(await confirmDialog({ title: `${action === "cancel" ? "Cancel" : "Remove"} ${jobSel.size} download(s)?`, body: action === "remove" ? "Server-mode downloads lose their files too." : "Unfinished tracks stop and their partial files are deleted.", confirmText: action === "cancel" ? "Cancel them" : "Remove", danger: true }))) return;
  try {
    const r = await withBusy(btn, () => post("/admin/jobs/bulk", { ids: [...jobSel], action }));
    toast(`${action}: ${r.affected} done${r.skipped ? ` · ${r.skipped} not applicable` : ""}`, "ok", { ms: 2500 });
    if (action === "remove") jobSel.clear();
    LOADERS.activity.load();
  } catch (e) { err(e); }
}

// ───────────────────────── visitors ─────────────────────────

LOADERS.visitors = {
  every: 10000,
  async load() {
    const [vis, bans] = await Promise.all([get("/admin/visitors"), get("/admin/bans")]);
    const banned = new Set(bans.map((b) => b.ip));
    $("#vis-count").textContent = `${vis.length} · ${vis.filter((v) => v.online).length} online`;
    $("#vis-table tbody").innerHTML = vis.map((v) => `<tr>
      <td><span class="vdot ${v.online ? "on" : ""}" title="${v.online ? "online" : "offline"}"></span></td>
      <td class="mono">${esc(v.short)}</td><td class="mono">${esc(v.ip)}${banned.has(v.ip) ? " <span class='badge text-bg-danger'>blocked</span>" : ""}</td>
      <td><small class="faint" title="${esc(v.ua)}">${esc(shortUa(v.ua))}</small></td><td class="mono">${n(v.requests)}</td>
      <td class="mono">${ago(v.first)}</td><td class="mono">${ago(v.last)}</td>
      <td>${banned.has(v.ip) ? "" : `<button class="btn-ghost sm danger" data-ban="${esc(v.ip)}"><i class="bi bi-slash-circle"></i>Block</button>`}</td></tr>`).join("");
    $("#vis-empty").hidden = vis.length > 0;
    $("#bans").innerHTML = bans.length ? bans.map((b) => `<li><i class="bi bi-slash-circle" style="color:var(--bad)"></i><div style="min-width:0"><b class="mono">${esc(b.ip)}</b><br><small>${esc(b.reason || "no reason")} · ${ago(b.at)}</small></div><button class="btn-ghost sm ms-auto" data-unban="${esc(b.ip)}">Unblock</button></li>`).join("") : `<li class="adm-dim">Nobody is blocked.</li>`;
  },
};

function shortUa(ua = "") {
  const b = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "Other";
  const os = /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Windows/.test(ua) ? "Windows" : /Mac OS/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  return os ? `${b} · ${os}` : b;
}

async function ban(ip, reason = "") {
  if (!(await confirmDialog({ title: `Block ${ip}?`, body: "Every request from this address will be refused.", confirmText: "Block", danger: true }))) return;
  try { await post("/admin/bans", { ip, reason }); toast(`${ip} blocked`, "ok"); LOADERS.visitors.load(); } catch (e) { err(e); }
}

// ───────────────────────── files ─────────────────────────

const F = { path: "", entries: [], sel: new Set(), sort: ["name", 1] };
const AUDIO = new Set(["mp3", "m4a", "opus", "flac", "wav", "ogg", "aac"]), VIDEO = new Set(["mp4", "mkv", "webm", "mov"]);
const TEXT = new Set(["txt", "log", "json", "srt", "vtt", "lrc", "nfo", "md", "csv"]);
const fileIcon = (e) => e.is_dir ? (e.hidden ? "folder-x" : "folder-fill") : AUDIO.has(e.ext) ? "music-note-beamed" : VIDEO.has(e.ext) ? "film" : TEXT.has(e.ext) ? "file-earmark-text" : e.ext === "zip" ? "file-earmark-zip" : "file-earmark";

LOADERS.files = {
  async load() {
    const [entries, usage] = await Promise.all([get("/admin/files" + qs({ path: F.path, hidden: $("#f-hidden").checked })), get("/admin/files/usage")]);
    F.entries = entries;
    for (const p of [...F.sel]) if (!entries.some((e) => e.path === p)) F.sel.delete(p);
    renderFiles();
    const max = Math.max(1, ...usage.map((u) => u.size));
    $("#f-usage").innerHTML = usage.length ? usage.map((u) => `<div class="usage-row ${u.hidden ? "hidden-entry" : ""}"><span class="truncate-1">${esc(u.name)}</span><b class="mono">${fmtBytes(u.size)}</b><div class="bar"><i style="width:${(u.size / max) * 100}%"></i></div></div>`).join("") : `<div class="adm-dim">Empty.</div>`;
  },
};

function renderFiles() {
  const parts = F.path ? F.path.split("/") : [];
  let acc = "";
  $("#f-crumbs").innerHTML = [`<button data-path="" class="${parts.length ? "" : "cur"}"><i class="bi bi-house-door-fill"></i> downloads</button>`,
    ...parts.map((seg, i) => { acc += (acc ? "/" : "") + seg; return `<i class="bi bi-chevron-right sep"></i><button data-path="${esc(acc)}" class="${i === parts.length - 1 ? "cur" : ""}">${esc(seg)}</button>`; })].join("");
  const [key, dir] = F.sort;
  const rows = [...F.entries].sort((a, b) => (b.is_dir - a.is_dir) || (key === "name" ? a.name.localeCompare(b.name) : (a[key] - b[key])) * dir);
  $("#f-table tbody").innerHTML = rows.map((e) => `<tr data-path="${esc(e.path)}" class="${F.sel.has(e.path) ? "sel" : ""} ${e.hidden ? "hidden-entry" : ""}">
    <td><input type="checkbox" class="form-check-input" ${F.sel.has(e.path) ? "checked" : ""}></td>
    <td class="f-name"><i class="bi bi-${fileIcon(e)}"></i>${esc(e.name)}</td>
    <td class="mono">${fmtBytes(e.size)}</td><td class="mono">${e.is_dir ? n(e.files) : ""}</td><td class="mono">${fmtDate(e.modified)}</td>
    <td><div class="row-act">${e.is_dir ? `<button class="btn-icon sm" data-act="zip" title="Download as ZIP"><i class="bi bi-file-earmark-zip"></i></button>`
      : `<button class="btn-icon sm" data-act="open" title="Preview"><i class="bi bi-eye"></i></button><a class="btn-icon sm" href="/api/admin/files/download${qs({ path: e.path })}" title="Download"><i class="bi bi-download"></i></a>`}
      <button class="btn-icon sm" data-act="rename" title="Rename"><i class="bi bi-pencil"></i></button>
      <button class="btn-icon sm bad" data-act="delete" title="Delete"><i class="bi bi-trash3"></i></button></div></td></tr>`).join("");
  $("#f-empty").hidden = rows.length > 0;
  $("#f-bulk").hidden = F.sel.size === 0;
  $("#f-sel").textContent = `${F.sel.size} selected`;
  $("#f-all").checked = rows.length > 0 && rows.every((e) => F.sel.has(e.path));
  $$("#f-table th[data-sort]").forEach((th) => { th.textContent = th.textContent.replace(/ [▲▼]$/, "") + (th.dataset.sort === key ? (dir > 0 ? " ▲" : " ▼") : ""); });
}

const openFolder = (p) => { F.path = p; F.sel.clear(); LOADERS.files.load().catch(err); };

async function preview(e) {
  const body = $("#pv-body");
  $("#pv-title").textContent = e.name;
  const url = `/api/admin/files/download${qs({ path: e.path, inline: true })}`;
  if (AUDIO.has(e.ext)) body.innerHTML = `<audio src="${esc(url)}" controls autoplay></audio>`;
  else if (VIDEO.has(e.ext)) body.innerHTML = `<video src="${esc(url)}" controls autoplay playsinline></video>`;
  else if (TEXT.has(e.ext)) { body.innerHTML = `<pre class="mono">${esc(await get(`/admin/files/text${qs({ path: e.path })}`))}</pre>`; }
  else if (["jpg", "jpeg", "png", "webp", "gif"].includes(e.ext)) body.innerHTML = `<img src="${esc(url)}" alt="" style="max-width:100%;border-radius:12px">`;
  else { location.href = url.replace("&inline=true", ""); return; }
  const m = bootstrap.Modal.getOrCreateInstance($("#preview-modal"));
  $("#preview-modal").addEventListener("hidden.bs.modal", () => { body.innerHTML = ""; }, { once: true });
  m.show();
}

async function fileOp(op, paths, btn) {
  try {
    if (op === "zip") { location.href = "/api/admin/files/zip?" + paths.map((p) => "path=" + encodeURIComponent(p)).join("&"); return; }
    if (op === "rename") {
      const e = F.entries.find((x) => x.path === paths[0]);
      const name = await promptDialog({ title: `Rename ${e.is_dir ? "folder" : "file"}`, value: e.name, confirmText: "Rename" });
      if (!name || name === e.name) return;
      await post("/admin/files/rename", { path: e.path, name });
      toast("Renamed", "ok", { ms: 2000 });
    } else if (op === "move" || op === "copy") {
      const dest = await promptDialog({ title: `${op === "move" ? "Move" : "Copy"} ${paths.length} item(s) to…`, body: "A folder inside downloads (created if missing). Empty = the top level. Use / for sub-folders.", value: F.path, confirmText: op === "move" ? "Move" : "Copy" });
      if (dest === null) return;
      const r = await withBusy(btn, () => post(`/admin/files/${op}`, { paths, dest }));
      toast(`${r.done} ${op === "move" ? "moved" : "copied"}${r.errors.length ? ` · ${r.errors.length} failed: ${r.errors[0].error}` : ""}`, r.errors.length ? "warn" : "ok");
    } else if (op === "delete") {
      if (!(await confirmDialog({ title: `Delete ${paths.length} item(s)?`, body: "Files and folders are removed from disk permanently (hidden ones too). This cannot be undone.", confirmText: "Delete", danger: true }))) return;
      const r = await withBusy(btn, () => post("/admin/files/delete", { paths }));
      toast(`${r.done} deleted${r.errors.length ? ` · ${r.errors.length} failed` : ""}`, r.errors.length ? "warn" : "ok");
    }
    F.sel.clear();
    LOADERS.files.load();
  } catch (e) { err(e); }
}

// ───────────────────────── settings ─────────────────────────

const S = { knobs: [], dirty: {}, rt: null };

function knobInput(k, value) {
  const id = `k-${k.key.replace(/\W/g, "_")}`;
  if (k.kind === "bool") return `<div class="form-check form-switch m-0"><input class="form-check-input" type="checkbox" id="${id}" data-key="${k.key}" ${value ? "checked" : ""}></div>`;
  if (k.kind === "choice") return `<select class="form-select" id="${id}" data-key="${k.key}">${k.choices.map((c) => `<option ${String(value) === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  if (k.kind === "speed") return `<input class="form-control" id="${id}" data-key="${k.key}" value="${value ? esc(fmtSpeedInput(value)) : ""}" placeholder="none · e.g. 2M, 500K">`;
  if (k.kind === "int") return `<input class="form-control" type="number" id="${id}" data-key="${k.key}" value="${esc(String(value ?? ""))}" ${k.min != null ? `min="${k.min}"` : ""} ${k.max != null ? `max="${k.max}"` : ""}>`;
  return `<input class="form-control" id="${id}" data-key="${k.key}" value="${esc(value ?? "")}">`;
}
const fmtSpeedInput = (b) => (b % 1048576 === 0 ? `${b / 1048576}M` : b % 1024 === 0 ? `${b / 1024}K` : String(b));
const shown = (k, v) => k.kind === "speed" ? (v ? `${fmtBytes(v)}/s` : "none") : k.kind === "bool" ? (v ? "on" : "off") : (v ?? "—");

LOADERS.settings = {
  async load() {
    const [knobs, rt] = await Promise.all([get("/admin/config"), get("/runtime")]);
    S.knobs = knobs; S.rt = rt; S.dirty = {};
    const groups = [...new Set(knobs.map((k) => k.group))];
    $("#knobs").innerHTML = groups.map((g) => `<div class="glass adm-card knob-group"><div class="adm-card-h"><b>${esc(g)}</b></div><div class="adm-form-grid">${knobs.filter((k) => k.group === g).map((k) => `
      <div class="knob" data-key="${k.key}"><div class="knob-h">${esc(k.label)}${k.overridden ? `<span class="badge-ov">changed</span>` : ""}${k.server_only && !session.server_mode ? `<span class="badge-srv">server mode</span>` : ""}${k.overridden ? `<button class="reset" data-reset="${k.key}" title="Back to the .env value"><i class="bi bi-arrow-counterclockwise"></i> reset</button>` : ""}</div>
        ${knobInput(k, k.value)}${k.help ? `<p>${esc(k.help)}</p>` : ""}<span class="def">.env default: ${esc(String(shown(k, k.default)))}${k.max != null && k.kind === "int" ? ` · max ${k.max}` : ""}</span></div>`).join("")}</div></div>`).join("");
    const cap = rt.max_parallel_cap, ceil = rt.max_speed;
    $("#rt-form").innerHTML = `
      <div class="knob"><div class="knob-h">Parallel downloads now</div><input class="form-control" type="number" id="rt-par" min="1" max="${cap}" value="${rt.max_parallel}"><span class="def">hard ceiling ${cap} (change it below: “Max parallel downloads”)</span></div>
      <div class="knob"><div class="knob-h">Speed cap per download now</div><input class="form-control" id="rt-speed" value="${rt.speed_limit ? fmtSpeedInput(rt.speed_limit) : ""}" placeholder="none · e.g. 2M"><span class="def">${ceil ? `hard ceiling ${fmtBytes(ceil)}/s` : "no hard ceiling"}</span></div>
      <div class="knob"><div class="knob-h">&nbsp;</div><button class="btn-ghost" id="rt-apply"><i class="bi bi-check2"></i>Apply live limits</button><span class="def">takes effect for the next downloads</span></div>`;
    paintDirty();
  },
};

function parseSize(s) {
  const m = String(s || "").trim().match(/^(\d+(?:\.\d+)?)\s*([kmg]?)b?$/i);
  if (!m) return null;
  return Math.round(Number(m[1]) * { "": 1, k: 1024, m: 1048576, g: 1073741824 }[m[2].toLowerCase()]);
}

function onKnobChange(e) {
  const el = e.target.closest("[data-key]");
  if (!el || !el.matches("input, select")) return;
  const k = S.knobs.find((x) => x.key === el.dataset.key);
  let v = el.type === "checkbox" ? el.checked : el.value;
  if (k.kind === "int") v = v === "" ? null : Number(v);
  const box = el.closest(".knob");
  box.classList.remove("bad");
  if (k.kind === "int" && v != null && ((k.min != null && v < k.min) || (k.max != null && v > k.max))) { box.classList.add("bad"); toast(`${k.label}: ${k.min}–${k.max}`, "warn", { ms: 2500 }); }
  if (k.kind === "speed" && v && parseSize(v) == null) box.classList.add("bad");
  const same = k.kind === "speed" ? (parseSize(v) || null) === (k.value || null) : String(v) === String(k.value);
  if (same) delete S.dirty[k.key]; else S.dirty[k.key] = v;
  box.classList.toggle("dirty", !same);
  paintDirty();
}

function paintDirty() {
  const count = Object.keys(S.dirty).length;
  $("#savebar").hidden = count === 0;
  $("#dirty-n").textContent = String(count);
}

async function saveKnobs(btn) {
  if ($$("#knobs .knob.bad").length) return toast("Fix the highlighted values first.", "warn");
  try {
    await withBusy(btn, () => put("/admin/config", { values: S.dirty, reset: [] }));
    toast("Saved — the whole app uses the new values within a few seconds.", "ok", { ms: 3500 });
    LOADERS.settings.load();
  } catch (e) { err(e); }
}

async function applyRuntime(btn) {
  const par = Number($("#rt-par").value);
  const raw = $("#rt-speed").value.trim();
  const speed = raw ? parseSize(raw) : null;
  if (par > S.rt.max_parallel_cap) return toast(`Parallel downloads can't exceed the hard ceiling of ${S.rt.max_parallel_cap}.`, "warn");
  if (raw && speed == null) return toast("Speed: use a value like 2M or 500K.", "warn");
  if (speed && S.rt.max_speed && speed > S.rt.max_speed) return toast(`The speed cap can't exceed the hard ceiling of ${fmtBytes(S.rt.max_speed)}/s.`, "warn");
  try { await withBusy(btn, () => put("/runtime", { max_parallel: par, speed_limit: speed })); toast("Live limits applied.", "ok"); LOADERS.settings.load(); } catch (e) { err(e); }
}

// ───────────────────────── cookies ─────────────────────────

LOADERS.cookies = {
  async load() {
    const c = await get("/cookies");
    const ok = c.loaded && c.login && !c.warnings.length;
    $("#ck-status").innerHTML = c.loaded
      ? `<i class="bi bi-${ok ? "check-circle-fill" : "exclamation-triangle-fill"}" style="color:var(--${ok ? "ok" : "warn"})"></i> <b>${c.count} cookies</b> · ${c.login ? "signed in" : "not signed in"}${c.expires_at ? ` · login expires ${new Date(c.expires_at).toLocaleString()}` : ""}${c.warnings.length ? `<br><small class="faint">${esc(c.warnings.join(" "))}</small>` : ""}`
      : `<i class="bi bi-x-circle-fill" style="color:var(--bad)"></i> No server cookies loaded.`;
    $("#ck-scope").textContent = session.server_mode ? "used for visitors only if sharing is on" : "used for every request";
  },
};

// ───────────────────────── maintenance ─────────────────────────

LOADERS.maintenance = {
  async load() {
    const [actions, knobs] = await Promise.all([get("/admin/maintenance"), get("/admin/config")]);
    const on = knobs.find((k) => k.key === "server.maintenance"), msg = knobs.find((k) => k.key === "server.maintenance_message");
    $("#maint-pill").hidden = !on.value;
    $("#maint-form").innerHTML = `<div class="flex items-center gap-3 flex-wrap">
      <div class="form-check form-switch m-0"><input class="form-check-input" type="checkbox" id="mt-on" ${on.value ? "checked" : ""} style="width:3em;height:1.5em"></div>
      <b id="mt-state">${on.value ? "On — new downloads are refused" : "Off"}</b></div>
      <label class="field-label mt-3" for="mt-msg">Message shown to visitors</label>
      <div class="flex gap-2"><input class="form-control" id="mt-msg" value="${esc(msg.value || "")}" maxlength="300"><button class="btn-ghost" id="mt-save">Save message</button></div>
      ${session.server_mode ? "" : `<p class="faint small mt-2 mb-0">Only applies in server mode.</p>`}`;
    $("#actions").innerHTML = actions.map((a) => `<div class="act-card glass"><b>${esc(a.label)}</b><p>${esc(a.help)}</p><div class="act-result" data-res="${a.action}"></div>
      <button class="btn-ghost sm ${/clear-stats|expire|cookies|finished/.test(a.action) ? "danger" : ""}" data-run="${a.action}"><i class="bi bi-play-fill"></i>Run</button></div>`).join("");
  },
};

async function runAction(action, btn) {
  const label = btn.closest(".act-card").querySelector("b").textContent;
  if (/clear-stats|expire|cookies|finished/.test(action) && !(await confirmDialog({ title: `${label}?`, body: btn.closest(".act-card").querySelector("p").textContent, confirmText: "Run", danger: true }))) return;
  try {
    const r = await withBusy(btn, () => post(`/admin/maintenance/${action}`));
    $(`[data-res="${action}"]`).textContent = `✓ ${r.affected} affected · ${new Date().toLocaleTimeString()}`;
    toast(r.message, "ok", { ms: 2500 });
  } catch (e) { err(e); }
}

// ───────────────────────── logs ─────────────────────────

LOADERS.logs = {
  every: 3000,
  async load() {
    const sel = $("#log-file");
    if (!sel.options.length) {
      const files = await get("/admin/logs");
      sel.innerHTML = files.map((f) => `<option value="${esc(f.name)}">${esc(f.name)} · ${fmtBytes(f.size)}</option>`).join("") || `<option value="">no log files</option>`;
      const pick = files.find((f) => f.name === "worker.log") || files[0];
      if (pick) sel.value = pick.name;
    }
    if (!sel.value || (!$("#log-follow").checked && $("#log-out").dataset.loaded)) return;
    const r = await get(`/admin/logs/${encodeURIComponent(sel.value)}` + qs({ lines: $("#log-lines").value, level: $("#log-level").value, q: $("#log-q").value }));
    const out = $("#log-out");
    const atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
    out.innerHTML = r.lines.map((l) => {
      const cls = /\b(ERROR|CRITICAL)\b|Traceback/.test(l) ? "l-err" : /\bWARNING\b/.test(l) ? "l-warn" : /\bSUCCESS\b|completed/.test(l) ? "l-ok" : /\bDEBUG\b/.test(l) ? "l-dim" : "";
      return `<span class="${cls}">${esc(l)}</span>`;
    }).join("\n") || "<span class='l-dim'>(empty)</span>";
    out.dataset.loaded = "1";
    if (atBottom || !out.dataset.scrolled) { out.scrollTop = out.scrollHeight; out.dataset.scrolled = "1"; }
  },
};

// ───────────────────────── wiring ─────────────────────────

function init() {
  $("#login-form").addEventListener("submit", login);
  $("#logout").addEventListener("click", async () => { try { await post("/admin/logout"); } catch { /* ignore */ } session = { ...session, admin: false }; showLogin(); });
  window.addEventListener("hashchange", () => { if (!$("#shell").hidden) route(); });
  $("#menu").addEventListener("click", openMenu);
  $("#scrim").addEventListener("click", closeMenu);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("#shell").hidden) LOADERS[view]?.load().catch(err); });

  // activity
  $("#job-filter").addEventListener("click", (e) => { const b = e.target.closest("[data-s]"); if (!b) return; jobFilter = b.dataset.s; $$("#job-filter button").forEach((x) => x.classList.toggle("active", x === b)); LOADERS.activity.load().catch(err); });
  $("#jobs-table").addEventListener("change", (e) => {
    if (e.target.id === "jobs-all") { $$("#jobs-table tbody tr").forEach((tr) => (e.target.checked ? jobSel.add(tr.dataset.id) : jobSel.delete(tr.dataset.id))); }
    else { const tr = e.target.closest("tr[data-id]"); if (tr) e.target.checked ? jobSel.add(tr.dataset.id) : jobSel.delete(tr.dataset.id); }
    LOADERS.activity.load().catch(err);
  });
  $("#job-bulk").addEventListener("click", (e) => { const b = e.target.closest("[data-bulk]"); if (b) jobBulk(b.dataset.bulk, b); });

  // visitors
  $("#vis-table").addEventListener("click", (e) => { const b = e.target.closest("[data-ban]"); if (b) ban(b.dataset.ban, "from the visitor list"); });
  $("#bans").addEventListener("click", async (e) => { const b = e.target.closest("[data-unban]"); if (!b) return; try { await del(`/admin/bans/${encodeURIComponent(b.dataset.unban)}`); toast("Unblocked", "ok"); LOADERS.visitors.load(); } catch (ex) { err(ex); } });
  $("#ban-form").addEventListener("submit", (e) => { e.preventDefault(); ban($("#ban-ip").value.trim(), $("#ban-reason").value.trim()).then(() => { $("#ban-ip").value = ""; $("#ban-reason").value = ""; }); });

  // files
  $("#f-crumbs").addEventListener("click", (e) => { const b = e.target.closest("[data-path]"); if (b) openFolder(b.dataset.path); });
  $("#f-hidden").addEventListener("change", () => LOADERS.files.load().catch(err));
  $("#f-refresh").addEventListener("click", (e) => withBusy(e.currentTarget, () => LOADERS.files.load()).catch(err));
  $("#f-mkdir").addEventListener("click", async () => {
    const name = await promptDialog({ title: "New folder", body: `Inside downloads/${F.path ? F.path + "/" : ""}`, confirmText: "Create" });
    if (!name) return;
    try { await post("/admin/files/mkdir", { path: (F.path ? F.path + "/" : "") + name }); toast("Folder created", "ok", { ms: 2000 }); LOADERS.files.load(); } catch (e) { err(e); }
  });
  $("#f-table").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]");
    if (th) { F.sort = [th.dataset.sort, F.sort[0] === th.dataset.sort ? -F.sort[1] : 1]; return renderFiles(); }
    const tr = e.target.closest("tr[data-path]");
    if (!tr || e.target.matches("input")) return;
    const entry = F.entries.find((x) => x.path === tr.dataset.path);
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "zip") return fileOp("zip", [entry.path]);
    if (act === "rename") return fileOp("rename", [entry.path]);
    if (act === "delete") return fileOp("delete", [entry.path], e.target.closest("button"));
    if (act === "open") return preview(entry).catch(err);
    if (e.target.closest("a")) return;
    if (e.target.closest(".f-name")) return entry.is_dir ? openFolder(entry.path) : preview(entry).catch(err);
  });
  $("#f-table").addEventListener("change", (e) => {
    if (e.target.id === "f-all") F.entries.forEach((x) => (e.target.checked ? F.sel.add(x.path) : F.sel.delete(x.path)));
    else { const tr = e.target.closest("tr[data-path]"); if (tr) e.target.checked ? F.sel.add(tr.dataset.path) : F.sel.delete(tr.dataset.path); }
    renderFiles();
  });
  $("#f-bulk").addEventListener("click", (e) => {
    const b = e.target.closest("[data-fop]");
    if (!b) return;
    const paths = [...F.sel];
    if (b.dataset.fop === "rename" && paths.length !== 1) return toast("Select exactly one item to rename.", "info");
    fileOp(b.dataset.fop, paths, b);
  });

  // settings
  $("#knobs").addEventListener("input", onKnobChange);
  $("#knobs").addEventListener("change", onKnobChange);
  $("#knobs").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-reset]");
    if (!b) return;
    try { await put("/admin/config", { values: {}, reset: [b.dataset.reset] }); toast("Back to the .env value", "ok", { ms: 2000 }); LOADERS.settings.load(); } catch (ex) { err(ex); }
  });
  $("#save").addEventListener("click", (e) => saveKnobs(e.currentTarget));
  $("#discard").addEventListener("click", () => LOADERS.settings.load().catch(err));
  $("#rt-form").addEventListener("click", (e) => { if (e.target.closest("#rt-apply")) applyRuntime(e.target.closest("#rt-apply")); });

  // cookies
  $("#ck-save").addEventListener("click", async (e) => {
    try { const c = await withBusy(e.currentTarget, () => put("/cookies", { text: $("#ck-text").value })); $("#ck-text").value = ""; toast(c.warnings.join(" ") || "Cookies saved.", c.login ? "ok" : "warn"); LOADERS.cookies.load(); } catch (ex) { err(ex); }
  });
  $("#ck-testbtn").addEventListener("click", async (e) => {
    $("#ck-test").textContent = "Checking with YouTube…";
    try { const r = await withBusy(e.currentTarget, () => post("/cookies/test")); $("#ck-test").innerHTML = `<i class="bi bi-${r.ok ? "check-circle-fill" : "x-circle-fill"}" style="color:var(--${r.ok ? "ok" : "bad"})"></i> ${esc(r.message)} <span class="faint">(${r.took_ms} ms)</span>`; } catch (ex) { $("#ck-test").textContent = ex.detail; }
  });
  $("#ck-clear").addEventListener("click", async (e) => {
    if (!(await confirmDialog({ title: "Remove the server cookies?", confirmText: "Remove", danger: true }))) return;
    try { await withBusy(e.currentTarget, () => del("/cookies")); toast("Removed", "ok"); LOADERS.cookies.load(); } catch (ex) { err(ex); }
  });

  // maintenance
  $("#actions").addEventListener("click", (e) => { const b = e.target.closest("[data-run]"); if (b) runAction(b.dataset.run, b); });
  $("#maint-form").addEventListener("change", async (e) => {
    if (e.target.id !== "mt-on") return;
    const on = e.target.checked;
    if (on && !(await confirmDialog({ title: "Turn maintenance mode on?", body: "Visitors cannot start new downloads until you turn it off. Running downloads finish.", confirmText: "Turn on", danger: true }))) { e.target.checked = false; return; }
    try { await put("/admin/config", { values: { "server.maintenance": on }, reset: [] }); toast(on ? "Maintenance mode on" : "Maintenance mode off", on ? "warn" : "ok"); LOADERS.maintenance.load(); } catch (ex) { err(ex); e.target.checked = !on; }
  });
  $("#maint-form").addEventListener("click", async (e) => {
    if (e.target.id !== "mt-save") return;
    try { await put("/admin/config", { values: { "server.maintenance_message": $("#mt-msg").value }, reset: [] }); toast("Message saved", "ok", { ms: 2000 }); } catch (ex) { err(ex); }
  });

  // logs
  for (const id of ["#log-file", "#log-level", "#log-lines"]) $(id).addEventListener("change", () => { delete $("#log-out").dataset.scrolled; LOADERS.logs.load().catch(err); });
  let qTimer;
  $("#log-q").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(() => LOADERS.logs.load().catch(err), 400); });

  boot();
}

init();
