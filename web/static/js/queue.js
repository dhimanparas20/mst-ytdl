// Downloads panel: stats, bulk toolbar, filters and the keyed job list.
//
// The server pushes a full snapshot ~1/s while anything changes. Re-rendering
// that would reset scroll positions, expanded rows and CSS animations, so every
// job/item owns a DOM node that is created once and then patched in place.

import { api } from "./api.js";
import { confirmDialog, copyText, toast, withBusy, initTooltips } from "./ui.js";
import { fileTrack, isMediaName } from "./player.js";
import { addSpeedSample, speedAverage } from "./queue-logic.js";
import {
  $, $$, esc, htmlToEl, fmtBytes, fmtDuration, fmtEta, setText, setAttr, splitSpeed, tween,
} from "./util.js";

const LABEL = {
  queued: "Queued", resolving: "Reading playlist", downloading: "Downloading", processing: "Processing",
  paused: "Paused", completed: "Completed", failed: "Failed", cancelled: "Cancelled", partial: "Partial",
};
// Quick buttons per status. "retry" resumes from the partial file; "restart" starts over and
// replaces any existing file; "remove" only drops the entry (files stay).
const ACTIONS = {
  queued: ["pause", "cancel"], resolving: ["cancel"], downloading: ["pause", "cancel"], processing: ["pause", "cancel"],
  paused: ["resume", "cancel", "restart"], failed: ["retry", "restart", "remove"], cancelled: ["retry", "restart", "remove"],
  partial: ["retry", "restart", "remove"], completed: ["restart", "remove"],
};
const BTN = {
  pause: ["pause-fill", "warn", "Pause"], resume: ["play-fill", "ok", "Resume"],
  cancel: ["x-lg", "bad", "Cancel"], retry: ["arrow-clockwise", "info", "Retry (continue where it stopped)"],
  restart: ["arrow-repeat", "info", "Restart from scratch / replace the file"], remove: ["x-circle", "bad", "Remove from list"],
};
const QUICK = Object.keys(BTN);
const ITEM_QUICK = QUICK.filter((a) => a !== "remove");  // tracks inside a playlist cannot be removed one by one

const selected = new Set();      // job ids ticked in select mode
let selectMode = false;
const BULK_TEXT = {
  pause: "Pause", resume: "Resume", cancel: "Cancel", retry: "Retry", restart: "Restart / replace",
  remove: "Remove from list", "remove-files": "Remove + delete files",
};
const BULK_CONFIRM = {
  cancel: { title: "Cancel the selected downloads?", body: "Unfinished ones are cancelled and their partial files deleted.", confirmText: "Cancel them", danger: true },
  remove: { title: "Remove from the list?", body: "The entries disappear from this list. Files stay on disk.", confirmText: "Remove" },
  "remove-files": { title: "Remove and delete files?", body: "The entries AND their downloaded files are permanently deleted.", confirmText: "Delete", danger: true },
  restart: { title: "Restart / replace?", body: "Each one is downloaded again from scratch and overwrites its existing file.", confirmText: "Restart" },
};
const AUDIO = new Set(["mp3", "m4a", "opus", "flac", "wav"]);
const TERMINAL = new Set(["completed", "failed", "cancelled", "partial"]);
const FILTERS = [
  ["all", "All", () => true],
  ["active", "Active", (j) => ["downloading", "processing", "resolving"].includes(j.status)],
  ["queued", "Queued", (j) => j.status === "queued"],
  ["paused", "Paused", (j) => j.status === "paused"],
  ["completed", "Done", (j) => j.status === "completed"],
  ["failed", "Failed", (j) => ["failed", "partial", "cancelled"].includes(j.status)],
];

const cards = new Map(); // job_id -> card state
let filter = "all";
let firstSnapshot = true;
const lastStatus = new Map();
const speedHist = Array(60).fill(0);
// average / peak speed for this browser session (survives a reload, gone when the tab closes)
const SPEED_KEY = "mst.speedstats";
let speedStats = { sum: 0, n: 0, peak: 0 };
try { speedStats = addSpeedSample(JSON.parse(sessionStorage.getItem(SPEED_KEY) || "null"), 0); } catch { /* private mode */ }
const fmtRate = (v) => (v > 0 ? `${fmtBytes(v)}/s` : "–");
function paintSpeedStats() {
  setText($("#v-avg"), fmtRate(speedAverage(speedStats)));
  setText($("#v-peak"), fmtRate(speedStats.peak));
}
let jobsEl, emptyEl;
let lastJobs = [];
let serverMode = false;
// jobs whose file / ZIP this tab already handed to the browser (so a reload never downloads twice)
const AUTO_KEY = "mst.autosaved";
let autoSaved = new Set();
try { autoSaved = new Set(JSON.parse(sessionStorage.getItem(AUTO_KEY) || "[]")); } catch { /* private mode */ }

/** Server mode (or not): changes how finished jobs are saved. */
export function setQueueMode(cfg) { serverMode = !!cfg?.server_mode; }

function saveUrl(url) {
  const a = document.createElement("a");
  a.href = url; a.download = "";
  document.body.appendChild(a); a.click(); a.remove();
}

/** "54 min left" for a delivery's expiry. */
function leftText(expiresAt) {
  const s = Math.max(0, expiresAt - Date.now() / 1000);
  if (s < 60) return "expires in under a minute";
  const m = Math.round(s / 60);
  return m >= 60 ? `available for ${Math.floor(m / 60)} h ${m % 60} min` : `available for ${m} min`;
}

// ───────────────────────── helpers ─────────────────────────

const badge = (o) => {
  const f = o.format.toUpperCase();
  if (AUDIO.has(o.format)) return ["mp3", "m4a"].includes(o.format) && o.quality !== "best" ? `${f} ${o.quality}k` : f;
  return o.quality === "best" ? f : `${f} ${o.quality}p`;
};

function actionBtn(act, extra = "") {
  const [icon, cls, label] = BTN[act];
  return `<button type="button" class="btn-icon ${cls} ${extra}" data-act="${act}" aria-label="${label}" data-bs-toggle="tooltip" title="${label}" hidden><i class="bi bi-${icon}"></i></button>`;
}

function itemHTML(it) {
  return `
  <li class="item" data-item="${esc(it.item_id)}">
    <span class="i-idx mono"></span>
    <div style="min-width:0">
      <div class="i-title truncate-1"></div>
      <div class="pbar thin"><i></i></div>
      <div class="i-meta"><span class="st-pill sm"><i class="dot"></i><span class="pt"></span></span><span class="m-speed mono"></span><span class="m-eta mono"></span><span class="m-dur mono"></span><span class="err truncate-1" style="max-width:100%"></span></div>
    </div>
    <div class="i-actions">
      ${ITEM_QUICK.map((a) => actionBtn(a, "sm")).join("")}
      <button type="button" class="btn-icon sm info" data-act="play" aria-label="Play" data-bs-toggle="tooltip" title="Play" hidden><i class="bi bi-play-circle"></i></button>
      <button type="button" class="btn-icon sm ok" data-act="save" aria-label="Save file" data-bs-toggle="tooltip" title="Save file" hidden><i class="bi bi-download"></i></button>
    </div>
  </li>`;
}

function cardHTML(job) {
  return `
  <article class="job glass lift" data-id="${esc(job.job_id)}" data-status="${esc(job.status)}">
    <div class="job-main">
      <div class="thumb">
        <span class="sel-box" data-act="select" role="checkbox" aria-checked="false" aria-label="Select"><i class="bi bi-check-lg"></i></span>
        <img alt="" loading="lazy" decoding="async">
        <span class="fmt-badge"></span><span class="pl-badge" hidden></span><span class="dur" hidden></span>
      </div>
      <div class="job-body">
        <div class="job-top">
          <h3 class="job-title clamp-2"></h3>
          <span class="st-pill"><i class="dot"></i><span class="pt"></span></span>
        </div>
        <div class="job-sub">
          <span class="uploader truncate-1"></span>
          <span class="tag click folder-tag local-only" data-act="folder" hidden><i class="bi bi-folder2-open"></i><span></span></span>
        </div>
        <div class="pbar"><i></i></div>
        <div class="job-meta">
          <b class="m-pct mono"></b><span class="m-stage"></span><span class="m-speed mono"></span><span class="m-eta mono"></span><span class="m-size mono"></span><span class="m-counts"></span>
        </div>
        <div><button type="button" class="expander" data-act="expand" hidden><i class="bi bi-chevron-down"></i><span>Show tracks</span></button></div>
      </div>
      <div class="job-actions"><div class="row">
        ${QUICK.map((a) => actionBtn(a)).join("")}
        <button type="button" class="btn-icon info" data-act="play" aria-label="Play" data-bs-toggle="tooltip" title="Play" hidden><i class="bi bi-play-circle-fill"></i></button>
        <button type="button" class="btn-icon ok" data-act="save" aria-label="Save file" data-bs-toggle="tooltip" title="Save file" hidden><i class="bi bi-download"></i></button>
        <button type="button" class="btn-icon ok" data-act="zip" aria-label="Download as ZIP" data-bs-toggle="tooltip" title="Download as ZIP" hidden><i class="bi bi-file-earmark-zip"></i></button>
        <div class="dropdown">
          <button type="button" class="btn-icon" data-bs-toggle="dropdown" aria-expanded="false" aria-label="More"><i class="bi bi-three-dots"></i></button>
          <ul class="dropdown-menu dropdown-menu-end">
            <li><button class="dropdown-item" data-act="shuffle"><i class="bi bi-shuffle"></i>Shuffle play</button></li>
            <li class="local-only"><button class="dropdown-item" data-act="folder"><i class="bi bi-folder2-open"></i>Open in library</button></li>
            <li><button class="dropdown-item" data-act="copy"><i class="bi bi-link-45deg"></i>Copy link</button></li>
            <li><button class="dropdown-item" data-act="restart"><i class="bi bi-arrow-repeat"></i>Restart from scratch (replace)</button></li>
            <li><hr class="dropdown-divider"></li>
            <li><button class="dropdown-item" data-act="remove"><i class="bi bi-x-circle"></i>Remove from list</button></li>
            <li class="local-only"><button class="dropdown-item text-danger" data-act="remove-files"><i class="bi bi-trash3"></i>Remove + delete files</button></li>
          </ul>
        </div>
      </div></div>
    </div>
    <div class="job-error" hidden><i class="bi bi-exclamation-octagon-fill"></i><span></span></div>
    <div class="job-note" hidden></div>
    <div class="job-delivery" hidden><button type="button" class="btn-grad" data-act="deliver"><i class="bi bi-download"></i><span>Save</span></button><span class="mono d-size"></span><span class="exp"></span></div>
    <div class="job-items" hidden><ul class="items"></ul></div>
  </article>`;
}

function refsOf(el) {
  const q = (s) => el.querySelector(s);
  return {
    img: q(".thumb img"), fmt: q(".fmt-badge"), pl: q(".pl-badge"), dur: q(".dur"),
    title: q(".job-title"), pill: q(".job-top .st-pill"), pillText: q(".job-top .pt"),
    uploader: q(".uploader"), folder: q(".folder-tag"), folderText: q(".folder-tag span"),
    bar: q(".job-body > .pbar"), barFill: q(".job-body > .pbar > i"),
    pct: q(".m-pct"), stage: q(".m-stage"), speed: q(".m-speed"), eta: q(".m-eta"), size: q(".m-size"), counts: q(".m-counts"),
    expander: q(".expander"), expText: q(".expander span"),
    error: q(".job-error"), errorText: q(".job-error span"), note: q(".job-note"),
    items: q(".job-items"), list: q(".items"),
    deliv: q(".job-delivery"), delivLabel: q(".job-delivery [data-act=deliver] span"), delivSize: q(".job-delivery .d-size"), delivExp: q(".job-delivery .exp"),
    sel: q(".sel-box"),
    btn: Object.fromEntries([...QUICK, "play", "save", "zip"].map((a) => [a, q(`.job-actions > .row > [data-act="${a}"]`)])),
  };
}

// ───────────────────────── card lifecycle ─────────────────────────

function createCard(job) {
  const el = htmlToEl(cardHTML(job));
  const c = { id: job.job_id, el, refs: refsOf(el), job, items: job.items || null, open: false, rows: new Map(), detailStamp: 0 };
  c.refs.img.addEventListener("error", () => { c.refs.img.style.opacity = 0; });
  new bootstrap.Dropdown($('[data-bs-toggle="dropdown"]', el), { popperConfig: (d) => ({ ...d, strategy: "fixed" }) });
  initTooltips(el);
  cards.set(job.job_id, c);
  return c;
}

function pending(items) {
  const it = (items || []).find((i) => i.control);
  return it ? it.control : null;
}

function updateCard(c, job) {
  c.job = job;
  if (job.items) c.items = job.items;
  const items = c.items || [];
  const r = c.refs;
  const single = job.kind === "single";
  const it0 = single ? items[0] : null;
  const st = job.status;
  const ctl = pending(items);

  c.el.dataset.status = st;
  setText(r.title, job.title || job.url);
  setAttr(r.title, "title", job.title || job.url);
  if (job.thumbnail && r.img.getAttribute("src") !== job.thumbnail) { r.img.style.opacity = 1; r.img.src = job.thumbnail; }
  setText(r.fmt, badge(job.options));
  if (single && it0?.duration) { setText(r.dur, fmtDuration(it0.duration)); r.dur.hidden = false; } else r.dur.hidden = true;
  if (!single) { setText(r.pl, `${job.total_items} tracks`); r.pl.hidden = false; } else r.pl.hidden = true;

  // status pill
  const label = ctl === "pause" ? "Pausing…" : ctl === "cancel" ? "Cancelling…" : LABEL[st] || st;
  r.pill.className = `st-pill st-${st}`;
  setText(r.pillText, label);

  setText(r.uploader, job.uploader || "");
  r.uploader.hidden = !job.uploader;
  if (job.folder) { setText(r.folderText, `downloads/${job.folder}`); r.folder.hidden = false; } else r.folder.hidden = true;

  // progress bar
  const running = st === "downloading" || st === "processing";
  r.bar.className = `pbar s-${st}${running ? " active" : ""}${st === "resolving" ? " indeterminate" : ""}`;
  r.barFill.style.width = `${st === "completed" ? 100 : Math.max(0, Math.min(100, job.percent))}%`;

  // meta line
  const showPct = !["resolving", "failed", "cancelled"].includes(st) || job.percent > 0;
  setText(r.pct, st === "completed" ? "100%" : showPct ? `${job.percent.toFixed(job.percent >= 100 ? 0 : 1)}%` : "");
  r.pct.hidden = !r.pct.textContent;
  const stage = running && it0?.stage ? it0.stage : "";
  setText(r.stage, stage ? `· ${stage}` : "");
  setText(r.speed, running && job.speed ? `↓ ${fmtBytes(job.speed)}/s` : "");
  setText(r.eta, running && job.eta != null ? `ETA ${fmtEta(job.eta)}` : "");
  const dl = job.downloaded_bytes, tot = job.total_bytes;
  setText(r.size, st === "completed" && it0?.filesize ? fmtBytes(it0.filesize) : dl && (running || st === "paused") ? (tot ? `${fmtBytes(dl)} / ${fmtBytes(tot)}` : fmtBytes(dl)) : "");
  if (!single) {
    const k = job.counts || {};
    const bits = [`${k.completed || 0}/${job.total_items} done`];
    if (k.downloading || k.processing) bits.push(`${(k.downloading || 0) + (k.processing || 0)} active`);
    if (k.failed) bits.push(`${k.failed} failed`);
    if (k.paused) bits.push(`${k.paused} paused`);
    setText(r.counts, st === "resolving" ? "" : bits.join(" · "));
  } else setText(r.counts, "");

  // errors / notes
  let err = "";
  if (job.resolve_error) err = job.resolve_error;
  else if (single && it0?.error && ["failed"].includes(it0.status)) err = it0.error;
  else if (!single && st === "failed") err = items.find((i) => i.error)?.error || "All items failed";
  r.error.hidden = !err;
  setText(r.errorText, err);
  let note = "";
  if (single && it0?.error && ["queued", "paused"].includes(it0.status)) note = it0.error;
  else if (st === "partial") note = `${(job.counts?.failed || 0) + (job.counts?.cancelled || 0)} of ${job.total_items} did not finish — Retry will try them again.`;
  r.note.hidden = !note;
  setText(r.note, note);

  // action buttons
  const allowed = new Set(ACTIONS[st] || []);
  const busyCtl = !!ctl;
  QUICK.forEach((a) => { r.btn[a].hidden = !allowed.has(a) || (busyCtl && (a === "pause")); });
  const done = st === "completed";
  const canPlayPlaylist = !single && (job.counts?.completed || 0) > 0 && AUDIO.has(job.options.format);
  r.btn.play.hidden = !((done && single && it0?.filepath) || canPlayPlaylist);
  r.btn.save.hidden = serverMode || !(done && single && it0?.filepath);
  const isSel = selected.has(c.id);
  c.el.classList.toggle("selected", isSel);
  r.sel.setAttribute("aria-checked", String(isSel));
  r.btn.zip.hidden = serverMode || !(!single && ["completed", "partial"].includes(st) && job.folder);

  // server mode: the prepared file / ZIP, with its expiry
  const d = serverMode && TERMINAL.has(st) ? job.delivery : null;
  r.deliv.hidden = !d;
  if (d) {
    setText(r.delivLabel, d.kind === "zip" ? "Save ZIP" : "Save file");
    setText(r.delivSize, fmtBytes(d.size));
    paintExpiry(r.delivExp, d.expires_at);
  }

  // expander for playlists
  r.expander.hidden = single;
  if (!single) setText(r.expText, c.open ? "Hide tracks" : `Show tracks (${job.total_items})`);
  c.el.classList.toggle("open", c.open);

  if (c.open) syncItems(c);
}

// ───────────────────────── playlist items ─────────────────────────

function syncItems(c) {
  const items = c.items || [];
  const r = c.refs;
  r.items.hidden = false;
  const seen = new Set();
  items.forEach((it, idx) => {
    seen.add(it.item_id);
    let row = c.rows.get(it.item_id);
    if (!row) {
      const el = htmlToEl(itemHTML(it));
      row = { el, q: (s) => el.querySelector(s) };
      row.refs = {
        idx: row.q(".i-idx"), title: row.q(".i-title"), bar: row.q(".pbar"), fill: row.q(".pbar > i"),
        pill: row.q(".st-pill"), pt: row.q(".pt"), speed: row.q(".m-speed"), eta: row.q(".m-eta"), dur: row.q(".m-dur"), err: row.q(".err"),
        btn: Object.fromEntries([...ITEM_QUICK, "play", "save"].map((a) => [a, row.q(`[data-act="${a}"]`)])),
      };
      initTooltips(el);
      c.rows.set(it.item_id, row);
    }
    updateRow(row, it);
    if (r.list.children[idx] !== row.el) r.list.insertBefore(row.el, r.list.children[idx] || null);
  });
  for (const [id, row] of c.rows) if (!seen.has(id)) { row.el.remove(); c.rows.delete(id); }
}

function updateRow(row, it) {
  const f = row.refs;
  row.el.dataset.status = it.status;
  setText(f.idx, String(it.index));
  setText(f.title, it.title || it.url);
  setAttr(f.title, "title", it.title || it.url);
  const running = it.status === "downloading" || it.status === "processing";
  f.bar.className = `pbar thin s-${it.status}${running ? " active" : ""}`;
  f.fill.style.width = `${it.status === "completed" ? 100 : Math.max(0, Math.min(100, it.percent))}%`;
  const label = it.control === "pause" ? "Pausing…" : it.control === "cancel" ? "Cancelling…" : (it.stage && it.status === "processing" ? it.stage : LABEL[it.status]);
  f.pill.className = `st-pill sm st-${it.status}`;
  setText(f.pt, label);
  setText(f.speed, running && it.speed ? `${fmtBytes(it.speed)}/s` : "");
  setText(f.eta, running && it.eta != null ? `ETA ${fmtEta(it.eta)}` : "");
  setText(f.dur, it.duration ? fmtDuration(it.duration) : "");
  setText(f.err, ["failed", "queued", "paused"].includes(it.status) && it.error ? it.error : "");
  const allowed = new Set(ACTIONS[it.status] || []);
  ITEM_QUICK.forEach((a) => { f.btn[a].hidden = !allowed.has(a) || (!!it.control && a === "pause"); });
  const hasFile = it.status === "completed" && it.filepath;
  f.btn.play.hidden = !hasFile;
  f.btn.save.hidden = !hasFile;
}

function paintExpiry(el, expiresAt) {
  setText(el, `· ${leftText(expiresAt)}`);
  el.classList.toggle("soon", expiresAt - Date.now() / 1000 < 600);
}

async function loadDetail(c) {
  try {
    const full = await api.job(c.id);
    c.items = full.items || [];
    c.detailStamp = full.updated_at;
    if (c.open) syncItems(c);
    updateCard(c, { ...c.job, items: c.items });
  } catch (e) { toast(e.detail || "Could not load tracks", "err"); }
}

// ───────────────────────── snapshot → DOM ─────────────────────────

export function applySnapshot({ jobs, stats }) {
  lastJobs = jobs;
  const seen = new Set();

  jobs.forEach((job, idx) => {
    seen.add(job.job_id);
    let c = cards.get(job.job_id);
    const fresh = !c;
    if (!c) c = createCard(job);
    // a finished playlist arrives without items; keep what we fetched, refetch if it changed
    const stale = c.open && !job.items && job.updated_at > c.detailStamp && TERMINAL.has(job.status);
    updateCard(c, job);
    if (stale) loadDetail(c);
    if (jobsEl.children[idx] !== c.el) jobsEl.insertBefore(c.el, jobsEl.children[idx] || null);
    if (fresh && firstSnapshot) c.el.style.animationDelay = `${Math.min(idx, 10) * 40}ms`;

    // toast on transitions (never for the initial load)
    const prev = lastStatus.get(job.job_id);
    maybeAutoSave(job, prev);
    if (!firstSnapshot && prev && prev !== job.status) {
      if (job.status === "completed") toast(job.title || job.url, "ok", { title: "Download finished" });
      else if (job.status === "failed") toast(job.resolve_error || job.items?.[0]?.error || job.title, "err", { title: "Download failed" });
      else if (job.status === "partial") toast(job.title, "warn", { title: "Finished with errors" });
    }
    lastStatus.set(job.job_id, job.status);
  });

  for (const [id, c] of [...cards]) {
    if (seen.has(id)) continue;
    cards.delete(id);
    lastStatus.delete(id);
    c.el.classList.add("leaving");
    setTimeout(() => c.el.remove(), 380);
  }
  firstSnapshot = false;

  applyFilter();
  renderFilters(jobs);
  if (stats) renderStats(stats);
}

/**
 * Hand a finished job to the browser once, if this tab saw it finish:
 * server mode — the prepared file / ZIP as soon as it is ready; personal mode — carts that asked for a ZIP.
 */
function maybeAutoSave(job, prev) {
  const had = lastDelivery.get(job.job_id);  // undefined = first time this tab sees the job
  lastDelivery.set(job.job_id, !!job.delivery);
  if (firstSnapshot || prev === undefined || autoSaved.has(job.job_id)) return;
  const ready = serverMode
    ? !!job.delivery && TERMINAL.has(job.status) && had === false
    : job.deliver_zip && ["completed", "partial"].includes(job.status) && !TERMINAL.has(prev);
  if (!ready) return;
  autoSaved.add(job.job_id);
  try { sessionStorage.setItem(AUTO_KEY, JSON.stringify([...autoSaved].slice(-200))); } catch { /* ignore */ }
  saveUrl(api.jobFileUrl(job.job_id));
  toast(job.title || "Your download", "ok", { title: job.delivery?.kind === "zip" || job.deliver_zip ? "ZIP ready — saving" : "Saving to your device", ms: 3500 });
}
const lastDelivery = new Map();

const visibleCards = () => [...cards.values()].filter((c) => !c.el.hidden);

function applyFilter() {
  const pred = FILTERS.find((f) => f[0] === filter)[2];
  let visible = 0;
  for (const c of cards.values()) {
    const show = pred(c.job);
    c.el.hidden = !show;
    if (show) visible++;
  }
  const none = cards.size === 0;
  emptyEl.hidden = visible > 0;
  setText($("#empty-text"), none ? "Paste a YouTube or YouTube Music link to get started." : "No downloads match this filter.");
  for (const id of [...selected]) if (!cards.has(id)) selected.delete(id);  // vanished jobs leave the selection
  renderBulkBar();
  renderFilterActions();
}

// ───────────────────────── select mode + bulk ─────────────────────────

function renderBulkBar() {
  const bar = $("#bulk-bar");
  bar.hidden = !(selectMode && selected.size > 0);
  setText($("#bulk-count"), `${selected.size} selected`);
  $("#select-toggle").setAttribute("aria-pressed", String(selectMode));
  $("#select-toggle").classList.toggle("active-toggle", selectMode);
  document.body.classList.toggle("select-mode", selectMode);
}

function setSelectMode(on) {
  selectMode = on;
  if (!on) selected.clear();
  for (const c of cards.values()) updateCard(c, c.job);
  renderBulkBar();
}

function toggleSelected(id) {
  selected.has(id) ? selected.delete(id) : selected.add(id);
  const c = cards.get(id);
  if (c) updateCard(c, c.job);
  renderBulkBar();
}

/** POST a bulk action for the given job ids and report the outcome. */
async function runBulk(ids, action, { clear = false, button } = {}) {
  if (!ids.length) return;
  const cfg = BULK_CONFIRM[action];
  if (cfg && !(await confirmDialog({ ...cfg, body: `${ids.length} download${ids.length === 1 ? "" : "s"} — ${cfg.body}` }))) return;
  try {
    const r = await withBusy(button, () => api.bulk(ids, action));
    const bits = [`${BULK_TEXT[action]}: ${r.affected} done`];
    if (r.skipped) bits.push(`${r.skipped} not applicable`);
    if (r.errors?.length) bits.push(`${r.errors.length} missing`);
    toast(bits.join(" · "), r.affected ? "ok" : "info", { ms: 3000 });
    if (clear) selected.clear();
    renderBulkBar();
  } catch (e) { toast(e.detail || e.message, "err"); }
}

/** The contextual bar under the filter chips: what you probably want to do with *this* list. */
function renderFilterActions() {
  const bar = $("#filter-actions");
  const ids = visibleCards().map((c) => c.id);
  const ctx = {
    failed: { cls: "bad", icon: "exclamation-triangle-fill", text: (n) => `${n} download${n === 1 ? "" : "s"} did not finish`, acts: [["retry", "Retry all", "arrow-clockwise"], ["restart", "Restart all", "arrow-repeat"], ["remove", "Remove all", "x-circle"]] },
    paused: { cls: "warn", icon: "pause-circle-fill", text: (n) => `${n} paused`, acts: [["resume", "Resume all", "play-fill"], ["cancel", "Cancel all", "x-lg"]] },
    completed: { cls: "ok", icon: "check2-circle", text: (n) => `${n} finished`, acts: [["restart", "Re-download all", "arrow-repeat"], ["remove", "Clear from list", "x-circle"]] },
    active: { cls: "", icon: "cloud-download", text: (n) => `${n} running`, acts: [["pause", "Pause these", "pause-fill"], ["cancel", "Cancel these", "x-lg"]] },
    queued: { cls: "", icon: "hourglass-split", text: (n) => `${n} waiting`, acts: [["pause", "Pause these", "pause-fill"], ["cancel", "Cancel these", "x-lg"]] },
  }[filter];
  if (!ctx || !ids.length) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.className = `ctx-bar glass mb-3 ${ctx.cls}`;
  const key = `${filter}:${ids.length}`;
  if (bar.dataset.key === key) return;  // unchanged — keep the DOM (and any hover state)
  bar.dataset.key = key;
  bar.innerHTML = `<div class="ctx-text"><i class="bi bi-${ctx.icon}"></i><b>${ctx.text(ids.length)}</b></div>
    <div class="flex flex-wrap gap-2 ms-auto">${ctx.acts.map(([a, label, icon]) => `<button class="btn-ghost sm ${["cancel", "remove"].includes(a) ? "danger" : ""}" data-ctx="${a}"><i class="bi bi-${icon}"></i>${label}</button>`).join("")}</div>`;
}

function renderFilters(jobs) {
  const host = $("#filters");
  if (!host.children.length) {
    host.innerHTML = FILTERS.map(([k, label]) => `<button type="button" class="chip" data-f="${k}">${label}<span class="n"></span></button>`).join("");
    host.addEventListener("click", (e) => {
      const b = e.target.closest("[data-f]");
      if (!b) return;
      filter = b.dataset.f;
      $("#filter-actions").dataset.key = "";
      applyFilter();
      renderFilters(lastJobs);
    });
  }
  FILTERS.forEach(([k, , pred]) => {
    const b = host.querySelector(`[data-f="${k}"]`);
    b.classList.toggle("active", k === filter);
    const n = jobs.filter(pred).length;
    const el = b.querySelector(".n");
    setText(el, String(n));
    el.classList.toggle("hot", n > 0 && (k === "failed" || k === "paused"));  // red / amber when something needs you
  });
}

// ───────────────────────── stats ─────────────────────────

let sparkCtx = null;
function renderStats(s) {
  const sp = splitSpeed(s.speed || 0);
  setText($("#v-speed"), sp.value);
  setText($("#u-speed"), sp.unit);
  for (const k of ["active", "queued", "paused", "completed", "failed"]) {
    const el = $(`#v-${k}`);
    const to = s[k] || 0;
    if (Number(el.dataset.v ?? -1) !== to) {
      tween(el, to);
      if (el.dataset.seen) { const tile = el.closest(".stat"); tile.classList.remove("bump"); void tile.offsetWidth; tile.classList.add("bump"); }
      el.dataset.seen = "1";
    }
  }
  speedHist.push(s.speed || 0); speedHist.shift();
  if (s.speed > 0) {
    speedStats = addSpeedSample(speedStats, s.speed);
    try { sessionStorage.setItem(SPEED_KEY, JSON.stringify(speedStats)); } catch { /* ignore */ }
    paintSpeedStats();
  }
  drawSpark();
  setPar(s.max_parallel);

  const badgeEl = $("#nav-badge");
  const active = (s.active || 0) + (s.queued || 0);
  badgeEl.hidden = !active;
  setText(badgeEl, String(active));
  document.title = s.active ? `↓ ${s.active} · ${sp.value} ${sp.unit} — mst-ytdl` : "mst-ytdl";
}

function drawSpark() {
  const cv = $("#spark");
  if (!sparkCtx) sparkCtx = cv.getContext("2d");
  const dpr = devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const ctx = sparkCtx;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const max = Math.max(...speedHist, 1);
  const pts = speedHist.map((v, i) => [(i / (speedHist.length - 1)) * w, h - 3 - (v / max) * (h - 8)]);
  const line = ctx.createLinearGradient(0, 0, w, 0);
  line.addColorStop(0, "#b3a6ff"); line.addColorStop(.55, "#8c7bf0"); line.addColorStop(1, "#6a58d6");
  const path = new Path2D();
  pts.forEach(([x, y], i) => {
    if (i === 0) path.moveTo(x, y);
    else { const [px, py] = pts[i - 1]; const mx = (px + x) / 2; path.bezierCurveTo(mx, py, mx, y, x, y); }
  });
  const fill = new Path2D(path);
  fill.lineTo(w, h); fill.lineTo(0, h); fill.closePath();
  const area = ctx.createLinearGradient(0, 0, 0, h);
  area.addColorStop(0, "rgba(106, 88, 214,.35)"); area.addColorStop(1, "rgba(106, 88, 214,0)");
  ctx.fillStyle = area; ctx.fill(fill);
  ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.strokeStyle = line; ctx.shadowColor = "rgba(106, 88, 214,.6)"; ctx.shadowBlur = 8;
  ctx.stroke(path);
  ctx.shadowBlur = 0;
}

// ───────────────────────── toolbar (parallel / cap / bulk) ─────────────────────────

let parCap = 12;
let parVal = 3;
let parTimer = null;
function setPar(v) {
  if (v == null || parTimer) return;  // don't fight the user's pending change
  parVal = v;
  setText($("#par-val"), String(v));
}
export function setParCap(n) { parCap = n; }

async function changePar(delta) {
  const next = Math.max(1, Math.min(parCap, parVal + delta));
  if (next === parVal) return;
  parVal = next;
  setText($("#par-val"), String(next));
  clearTimeout(parTimer);
  parTimer = setTimeout(async () => {
    parTimer = null;
    try { await api.setRuntime({ max_parallel: next }); } catch (e) { toast(e.detail, "err"); }
  }, 350);
}

export function showRuntime(rt) {
  setPar(rt.max_parallel);
  const sel = $("#rt-speed");
  const v = rt.speed_limit ? String(rt.speed_limit) : "";
  if (v && ![...sel.options].some((o) => o.value === v)) sel.add(new Option(`${fmtBytes(rt.speed_limit)}/s`, v));
  sel.value = v;
}

const BULK = {
  "pause-all": { ok: (n) => `Paused ${n} download${n === 1 ? "" : "s"}` },
  "resume-all": { ok: (n) => `Resumed ${n}` },
  "retry-failed": { ok: (n) => `Retrying ${n}` },
  "cancel-all": { confirm: { title: "Cancel everything?", body: "All unfinished downloads are cancelled and their partial files deleted.", confirmText: "Cancel all", danger: true }, ok: (n) => `Cancelled ${n}` },
  "clear-finished": { confirm: { title: "Clear finished?", body: "Removes finished entries from this list. Files stay in your downloads folder.", confirmText: "Clear list" }, ok: (n) => `Cleared ${n}` },
};

// ───────────────────────── click handling ─────────────────────────

/** Hand finished audio files to the mini player (queue = every finished audio track of the job). */
function playFromCard(c, item, opts = {}) {
  const items = (c.items || []).filter((i) => i.status === "completed" && i.filepath);
  const track = (i) => fileTrack({
    path: i.filepath, url: serverMode ? api.itemFileUrl(i.item_id) : null,
    title: i.title || c.job.title, artist: i.uploader || c.job.uploader || (serverMode ? "" : c.job.folder), thumb: i.thumbnail,
  });
  const audio = items.filter((i) => isMediaName(i.filepath));
  const target = item || audio[0] || items[0];
  if (!target) return toast("Nothing finished to play yet.", "info");
  // audio and video both play in the player
  const start = Math.max(0, audio.findIndex((i) => i.item_id === target.item_id));
  window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: audio.map(track), start, shuffle: !!opts.shuffle } }));
}

async function onClick(e) {
  const cardEl = e.target.closest(".job");
  if (!cardEl) return;
  const c = cards.get(cardEl.dataset.id);
  if (!c) return;
  const btn = e.target.closest("[data-act]");

  // select mode: a click anywhere on the card (outside real controls) ticks it
  if (selectMode) {
    const control = e.target.closest("button, a, .dropdown-menu, .items, input, .expander");
    if ((btn && btn.dataset.act === "select") || !control) { toggleSelected(c.id); return; }
  }
  if (!btn || btn.dataset.act === "select") return;

  const act = btn.dataset.act;
  const rowEl = btn.closest(".item");
  const itemId = rowEl?.dataset.item;
  const item = itemId ? (c.items || []).find((i) => i.item_id === itemId) : null;

  try {
    switch (act) {
      case "pause": case "resume": case "retry":
        await withBusy(btn, async () => {
          if (item) { const r = await api.itemAction(item.item_id, act); mergeItem(c, r); }
          else { const v = await api.jobAction(c.id, act); updateCard(c, v); }
        });
        break;
      case "restart": {
        const replacing = item ? item.status === "completed" : c.job.status === "completed" || c.job.status === "partial";
        if (replacing && !(await confirmDialog({ title: "Download again?", body: "It is downloaded from scratch and replaces the existing file once it succeeds.", confirmText: "Re-download" }))) return;
        await withBusy(btn, async () => {
          if (item) { const r = await api.itemAction(item.item_id, "restart"); mergeItem(c, r); }
          else { const v = await api.jobAction(c.id, "restart"); updateCard(c, v); }
        });
        toast("Restarted — fresh download queued", "info", { ms: 2200 });
        break;
      }
      case "cancel": {
        const many = !item && c.job.kind !== "single" && (c.job.total_items - (c.job.counts?.completed || 0)) > 1;
        if (many && !(await confirmDialog({ title: "Cancel this playlist?", body: "Unfinished tracks are cancelled and their partial files deleted.", confirmText: "Cancel tracks", danger: true }))) return;
        await withBusy(btn, async () => {
          if (item) { const r = await api.itemAction(item.item_id, "cancel"); mergeItem(c, r); }
          else { const v = await api.jobAction(c.id, "cancel"); updateCard(c, v); }
        });
        break;
      }
      case "expand":
        c.open = !c.open;
        c.refs.items.hidden = !c.open;
        if (c.open) { if (!c.items || TERMINAL.has(c.job.status)) await loadDetail(c); else syncItems(c); }
        updateCard(c, c.job);
        break;
      case "folder":
        window.dispatchEvent(new CustomEvent("open-folder", { detail: c.job.folder }));
        break;
      case "copy":
        await copyText(c.job.url); toast("Link copied", "ok", { ms: 2200 });
        break;
      case "shuffle": playFromCard(c, null, { shuffle: true }); break;
      case "play":
        playFromCard(c, item);
        break;
      case "save": {
        const it = item || c.items?.[0];
        if (!it?.filepath) return;
        saveUrl(serverMode ? api.itemFileUrl(it.item_id, true) : api.fileUrl(it.filepath, true));
        break;
      }
      case "zip":
        // a cart zips exactly its own files; a playlist keeps zipping its folder as before
        location.href = c.job.kind === "cart" ? api.jobFileUrl(c.id) : api.zipUrl(c.job.folder);
        break;
      case "deliver":
        autoSaved.add(c.id);
        saveUrl(api.jobFileUrl(c.id));
        break;
      case "remove":
        await api.remove(c.id, false); toast("Removed from list", "info", { ms: 2200 });
        break;
      case "remove-files":
        if (!(await confirmDialog({ title: "Delete files too?", body: "The downloaded files are permanently deleted from disk.", confirmText: "Delete", danger: true }))) return;
        { const r = await api.remove(c.id, true); toast(r.message || "Deleted", "ok", { ms: 2600 }); }
        break;
    }
  } catch (err) {
    toast(err.detail || err.message, "err");
  }
}

function mergeItem(c, updated) {
  if (!c.items) return;
  const i = c.items.findIndex((x) => x.item_id === updated.item_id);
  if (i >= 0) c.items[i] = updated;
  const row = c.rows.get(updated.item_id);
  if (row) updateRow(row, updated);
}

// ───────────────────────── init ─────────────────────────

export function initQueue() {
  jobsEl = $("#jobs");
  emptyEl = $("#empty");
  jobsEl.addEventListener("click", onClick);
  $("#par-minus").addEventListener("click", () => changePar(-1));
  $("#par-plus").addEventListener("click", () => changePar(+1));
  paintSpeedStats();
  $("#speed-reset").addEventListener("click", () => {
    speedStats = { sum: 0, n: 0, peak: 0 };
    try { sessionStorage.removeItem(SPEED_KEY); } catch { /* ignore */ }
    paintSpeedStats();
  });
  $("#rt-speed").addEventListener("change", async (e) => {
    try { await api.setRuntime({ speed_limit: e.target.value ? Number(e.target.value) : null }); toast(e.target.value ? "Speed cap applied to new downloads" : "Speed cap removed", "info", { ms: 2500 }); }
    catch (err) { toast(err.detail, "err"); }
  });
  $$("[data-q]").forEach((b) => b.addEventListener("click", async () => {
    const k = b.dataset.q, cfg = BULK[k];
    if (cfg.confirm && !(await confirmDialog(cfg.confirm))) return;
    try { const r = await withBusy(b, () => api.queue(k)); toast(cfg.ok(r.affected), r.affected ? "ok" : "info", { ms: 2400 }); }
    catch (err) { toast(err.detail, "err"); }
  }));
  // select mode + multi-select bar
  $("#select-toggle").addEventListener("click", () => setSelectMode(!selectMode));
  $("#bulk-all").addEventListener("click", () => { visibleCards().forEach((c) => selected.add(c.id)); for (const c of cards.values()) updateCard(c, c.job); renderBulkBar(); });
  $("#bulk-none").addEventListener("click", () => { selected.clear(); for (const c of cards.values()) updateCard(c, c.job); renderBulkBar(); });
  $("#bulk-bar").addEventListener("click", (e) => {
    const b = e.target.closest("[data-bulk]");
    if (b) runBulk([...selected], b.dataset.bulk, { clear: ["remove", "remove-files", "cancel"].includes(b.dataset.bulk), button: b });
  });
  // contextual actions for the current filter act on exactly what is listed
  $("#filter-actions").addEventListener("click", (e) => {
    const b = e.target.closest("[data-ctx]");
    if (b) runBulk(visibleCards().map((c) => c.id), b.dataset.ctx, { button: b });
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && selectMode && !e.target.matches?.("input, textarea")) setSelectMode(false);
  });
  renderFilters([]);
  applyFilter();
  setInterval(() => {
    if (!serverMode) return;
    for (const c of cards.values()) if (!c.refs.deliv.hidden && c.job.delivery) paintExpiry(c.refs.delivExp, c.job.delivery.expires_at);
  }, 30000);
  window.addEventListener("resize", () => { if (!$("#panel-queue").hidden) drawSpark(); });
}
