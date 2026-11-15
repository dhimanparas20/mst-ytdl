// Library tab: browse the downloads folder; play (mini player), save, zip, rename, move, delete —
// one at a time or for a multi-selection.

import { api } from "./api.js";
import { fileTrack } from "./player.js";
import { confirmDialog, promptDialog, toast, withBusy } from "./ui.js";
import { $, esc, fmtBytes, fmtDate, setText } from "./util.js";

const AUDIO = new Set(["mp3", "m4a", "opus", "flac", "wav", "ogg", "aac"]);
const VIDEO = new Set(["mp4", "mkv", "webm", "mov"]);

let path = "";
let entries = [];
let selectMode = false;
const selected = new Set();   // entry paths
let playingKey = null;
const canManage = () => !document.body.classList.contains("server-mode") || document.body.classList.contains("is-admin");

const iconFor = (e) => (e.is_dir ? ["folder2-open", "folder"] : VIDEO.has(e.ext) ? ["film", "video"] : AUDIO.has(e.ext) ? ["music-note-beamed", ""] : ["file-earmark", ""]);
const isAudio = (e) => !e.is_dir && (AUDIO.has(e.ext) || VIDEO.has(e.ext));
const isPlayable = (e) => !e.is_dir && (AUDIO.has(e.ext) || VIDEO.has(e.ext));
const tracksOf = (list) => list.filter(isAudio).map((e) => fileTrack({ path: e.path, title: e.name, artist: e.path.includes("/") ? e.path.split("/").slice(-2, -1)[0] : "Library" }));

/** Open the media modal (used for video; audio goes to the mini player). */
export function openPlayer({ title, path: p }) {
  window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: [fileTrack({ path: p, title })], start: 0 } }));
}
function _legacyOpenPlayer({ title, path: p }) {
  const ext = (p.split(".").pop() || "").toLowerCase();
  const url = api.fileUrl(p);
  $("#player-title").textContent = title || p.split("/").pop();
  const body = $("#player-body");
  if (VIDEO.has(ext)) body.innerHTML = `<video src="${esc(url)}" controls autoplay playsinline></video>`;
  else if (AUDIO.has(ext)) { window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: [fileTrack({ path: p, title })], start: 0 } })); return; }
  else { location.href = api.fileUrl(p, true); return; }
  const modalEl = $("#player-modal");
  const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
  modalEl.addEventListener("hidden.bs.modal", () => { body.innerHTML = ""; }, { once: true });  // stops playback
  modal.show();
}

function renderCrumbs() {
  const parts = path ? path.split("/") : [];
  let acc = "";
  const crumbs = [`<button data-path="" class="${parts.length ? "" : "cur"}"><i class="bi bi-house-door-fill"></i> downloads</button>`];
  parts.forEach((seg, i) => {
    acc += (acc ? "/" : "") + seg;
    crumbs.push(`<i class="bi bi-chevron-right sep"></i><button data-path="${esc(acc)}" class="${i === parts.length - 1 ? "cur" : ""}">${esc(seg)}</button>`);
  });
  $("#crumbs").innerHTML = crumbs.join("");
}

function render() {
  renderCrumbs();
  const grid = $("#lib-grid");
  $("#lib-empty").hidden = entries.length > 0;
  $("#lib-play-all").hidden = !entries.some(isAudio);
  grid.innerHTML = entries.map((e, i) => {
    const [icon, cls] = iconFor(e);
    const sel = selected.has(e.path);
    return `<div class="lib-item glass lift ${sel ? "selected" : ""} ${playingKey === `file:${e.path}` ? "is-playing" : ""}" data-i="${i}" data-key="file:${esc(e.path)}" style="animation-delay:${Math.min(i, 14) * 30}ms" tabindex="0">
      <span class="sel-box" data-a="select" role="checkbox" aria-checked="${sel}" aria-label="Select"><i class="bi bi-check-lg"></i></span>
      <div class="lib-ico ${cls}">${AUDIO.has(e.ext) && !e.is_dir ? `<img src="${esc(api.libCoverUrl(e.path))}" alt="" loading="lazy" onerror="this.remove()">` : ""}<i class="bi bi-${icon}"></i></div>
      <div style="min-width:0">
        <div class="lib-name clamp-2">${esc(e.name)}</div>
        <div class="lib-sub">${e.is_dir ? `${e.files} file${e.files === 1 ? "" : "s"} · ` : ""}${fmtBytes(e.size)} · ${fmtDate(e.modified)}</div>
      </div>
      <div class="lib-actions">
        ${isPlayable(e) ? `<button class="btn-icon sm info" data-a="play" aria-label="Play" title="Play"><i class="bi bi-play-fill"></i></button>` : ""}
        ${e.is_dir ? `<button class="btn-icon sm ok" data-a="zip" aria-label="Download as ZIP" title="Download as ZIP"><i class="bi bi-file-earmark-zip"></i></button>`
                   : `<button class="btn-icon sm ok" data-a="save" aria-label="Save" title="Save"><i class="bi bi-download"></i></button>`}
        <button class="btn-icon sm admin-ctl" data-a="rename" aria-label="Rename" title="Rename"><i class="bi bi-pencil"></i></button>
        <button class="btn-icon sm bad admin-ctl" data-a="delete" aria-label="Delete" title="Delete"><i class="bi bi-trash3"></i></button>
      </div>
    </div>`;
  }).join("");
  renderBulk();
}

function renderBulk() {
  document.body.classList.toggle("lib-select", selectMode);
  $("#lib-select-toggle").setAttribute("aria-pressed", String(selectMode));
  $("#lib-select-toggle").classList.toggle("active-toggle", selectMode);
  $("#lib-bulk").hidden = !(selectMode && selected.size > 0);
  $("#lib-bulk-count").textContent = `${selected.size} selected`;
}

function setSelectMode(on) {
  selectMode = on;
  if (!on) selected.clear();
  render();
}

export async function loadLibrary(p = path) {
  try {
    entries = await api.library(p);
    path = p;
    for (const s of [...selected]) if (!entries.some((e) => e.path === s)) selected.delete(s);
    render();
    const u = await api.libUsage();
    $("#lib-usage").innerHTML = `<i class="bi bi-hdd"></i>${u.files} file${u.files === 1 ? "" : "s"} · ${fmtBytes(u.bytes)}`;
    paintStorage(u);
  } catch (e) {
    toast(e.detail || e.message, "err", { title: "Library" });
    if (p) loadLibrary("");  // folder vanished — go back to the root
  }
}

export const currentPath = () => path;

/** The animated storage bar: library share, everything else on the disk, free space. */
function paintStorage(u) {
  const box = $("#storage");
  if (!u.disk_total) { box.hidden = true; return; }
  box.hidden = false;
  const total = u.disk_total;
  const lib = Math.min(u.bytes, u.disk_used);
  const pct = (n) => Math.max(0, Math.min(100, (n / total) * 100));
  const usedPct = pct(u.disk_used);
  // widths are set on the next frame so the bars grow from 0 (CSS transition) on first paint
  requestAnimationFrame(() => {
    $("#st-lib").style.width = `${pct(lib)}%`;
    $("#st-other").style.width = `${Math.max(0, usedPct - pct(lib))}%`;
  });
  const bar = $("#st-bar");
  bar.classList.toggle("warn", usedPct >= 80 && usedPct < 92);
  bar.classList.toggle("crit", usedPct >= 92);
  bar.setAttribute("aria-valuenow", String(Math.round(usedPct)));
  setText($("#st-used"), fmtBytes(u.disk_used));
  setText($("#st-total"), fmtBytes(total));
  setText($("#st-free"), `${fmtBytes(u.disk_free)} free`);
  setText($("#st-l-lib"), `Library ${fmtBytes(u.bytes)}`);
}

const download = (p) => {
  const a = document.createElement("a");
  a.href = api.fileUrl(p, true); a.download = "";
  document.body.appendChild(a); a.click(); a.remove();
};

async function rename(entry) {
  const name = await promptDialog({ title: `Rename ${entry.is_dir ? "folder" : "file"}`, value: entry.name, confirmText: "Rename" });
  if (!name || name === entry.name) return;
  try { await api.libRename(entry.path, name); toast("Renamed", "ok", { ms: 2000 }); await loadLibrary(path); }
  catch (e) { toast(e.detail || e.message, "err"); }
}

async function moveSelected(paths) {
  let folders = [];
  try { folders = await api.libFolders(); } catch { /* suggestions are optional */ }
  const dest = await promptDialog({
    title: `Move ${paths.length} item${paths.length === 1 ? "" : "s"}`,
    body: "Destination folder inside downloads (created if missing). Use / for sub-folders. Leave empty for the top level.",
    value: path, confirmText: "Move", suggestions: folders,
  });
  if (dest === null) return;
  try {
    const r = await api.libMove(paths, dest);
    toast(`Moved ${r.moved}${r.errors.length ? ` · ${r.errors.length} skipped (${r.errors[0].error})` : ""}`, r.moved ? "ok" : "warn", { ms: 3600 });
    selected.clear();
    await loadLibrary(path);
    window.dispatchEvent(new CustomEvent("folders-changed"));
  } catch (e) { toast(e.detail || e.message, "err"); }
}

async function deleteMany(paths, label) {
  const ok = await confirmDialog({ title: "Delete permanently?", body: `${label} will be removed from disk. This cannot be undone.`, confirmText: "Delete", danger: true });
  if (!ok) return;
  try {
    const r = await api.libBulkDelete(paths);
    toast(`Deleted ${r.deleted} item${r.deleted === 1 ? "" : "s"} (${r.files} file${r.files === 1 ? "" : "s"})${r.errors.length ? ` · ${r.errors.length} failed` : ""}`, "ok", { ms: 3000 });
    selected.clear();
    await loadLibrary(path);
    window.dispatchEvent(new CustomEvent("folders-changed"));
  } catch (e) { toast(e.detail || e.message, "err"); }
}

async function onGridClick(e) {
  const card = e.target.closest(".lib-item");
  if (!card) return;
  const entry = entries[+card.dataset.i];
  const act = e.target.closest("[data-a]")?.dataset.a;

  if (selectMode && (act === "select" || !e.target.closest("button"))) {
    selected.has(entry.path) ? selected.delete(entry.path) : selected.add(entry.path);
    card.classList.toggle("selected", selected.has(entry.path));
    card.querySelector(".sel-box").setAttribute("aria-checked", String(selected.has(entry.path)));
    renderBulk();
    return;
  }
  try {
    if (act === "play") return playEntry(entry);
    if (act === "save") return download(entry.path);
    if (act === "zip") { location.href = api.zipUrl(entry.path); return; }
    if ((act === "rename" || act === "delete") && !canManage()) return toast("Library changes need the admin password.", "warn");
    if (act === "rename") return rename(entry);
    if (act === "delete") return deleteMany([entry.path], `“${entry.name}”${entry.is_dir ? ` and its ${entry.files} file(s)` : ""}`);
    if (entry.is_dir) return loadLibrary(entry.path);
    if (isPlayable(entry)) return playEntry(entry);
    return download(entry.path);
  } catch (err) { toast(err.detail || err.message, "err"); }
}

/** Play a file: audio joins a queue of the folder's audio files (starting here); video opens the modal. */
function playEntry(entry) {
  if (entry.is_dir) return playFolder(entry.path);
  if (!isAudio(entry)) return openPlayer({ title: entry.name, path: entry.path });
  const audio = entries.filter(isAudio);
  window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: tracksOf(audio), start: audio.findIndex((x) => x.path === entry.path) } }));
}

async function playFolder(folder) {
  try {
    const list = folder === path ? entries : await api.library(folder);
    const tracks = tracksOf(list);
    if (!tracks.length) return toast("No audio files in that folder.", "info");
    window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks, start: 0 } }));
  } catch (e) { toast(e.detail || e.message, "err"); }
}

async function onBulk(action) {
  const paths = [...selected];
  const picked = entries.filter((e) => selected.has(e.path));
  if (!paths.length) return;
  if (action === "zip") { location.href = api.zipUrl(paths); return; }
  if ((action === "move" || action === "delete") && !canManage()) return toast("Library changes need the admin password.", "warn");
  if (action === "move") return moveSelected(paths);
  if (action === "delete") return deleteMany(paths, `${paths.length} selected item${paths.length === 1 ? "" : "s"}`);
  if (action === "play" || action === "queue") {
    const tracks = tracksOf(picked);
    const folderTracks = [];
    for (const e of picked.filter((x) => x.is_dir)) {  // selected folders contribute their audio files
      try { folderTracks.push(...tracksOf(await api.library(e.path))); } catch { /* skip unreadable folder */ }
    }
    const all = [...tracks, ...folderTracks];
    if (!all.length) return toast("The selection has no audio files.", "info");
    window.dispatchEvent(new CustomEvent(action === "play" ? "player-play" : "player-enqueue", { detail: { tracks: all, start: 0 } }));
  }
}

export function initLibrary() {
  $("#lib-grid").addEventListener("click", onGridClick);
  $("#lib-grid").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.classList.contains("lib-item")) onGridClick(e); });
  $("#crumbs").addEventListener("click", (e) => { const b = e.target.closest("[data-path]"); if (b) loadLibrary(b.dataset.path); });
  $("#lib-refresh").addEventListener("click", (e) => withBusy(e.currentTarget, () => loadLibrary(path)));
  $("#lib-play-all").addEventListener("click", () => playFolder(path));
  $("#lib-select-toggle").addEventListener("click", () => setSelectMode(!selectMode));
  $("#lib-bulk-all").addEventListener("click", () => { entries.forEach((e) => selected.add(e.path)); render(); });
  $("#lib-bulk-none").addEventListener("click", () => { selected.clear(); render(); });
  $("#lib-bulk").addEventListener("click", (e) => { const b = e.target.closest("[data-lbulk]"); if (b) onBulk(b.dataset.lbulk); });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && selectMode && !e.target.matches?.("input, textarea")) setSelectMode(false);
  });
  window.addEventListener("player-change", (e) => {
    playingKey = e.detail.playing ? e.detail.key : playingKey && e.detail.key === playingKey ? e.detail.key : null;
    document.querySelectorAll("#lib-grid .lib-item").forEach((el) => el.classList.toggle("is-playing", !!e.detail.key && el.dataset.key === e.detail.key));
  });
}
