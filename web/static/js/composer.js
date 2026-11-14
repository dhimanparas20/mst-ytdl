// "New download" card: link / search input, format + quality pickers, option
// switches, URL preview (with playlist track selection) and submission.

import { api } from "./api.js";
import { ytTrack } from "./player.js";
import { toast, withBusy } from "./ui.js";
import {
  $, $$, esc, htmlToEl, debounce, extractUrls, fmtDuration, compressSelection, parseSelection, store,
} from "./util.js";

const FORMATS = { audio: ["mp3", "m4a", "opus", "flac", "wav"], video: ["mp4", "mkv", "webm"] };
const FORMAT_HINT = { mp3: "most compatible", m4a: "AAC, small & clean", opus: "original, best size/quality", flac: "lossless", wav: "uncompressed", mp4: "H.264, plays everywhere", mkv: "any codec, flexible", webm: "VP9 / Opus" };
const LOSSY = new Set(["mp3", "m4a"]);
const NO_COVER = new Set(["wav", "webm"]); // yt-dlp cannot embed artwork into these
const HEIGHT_LABEL = { 1440: "1440p", 2160: "4K" };

const S = {
  src: "link", kind: "audio", format: "mp3", q: { audio: "192", video: "1080" },
  o: { thumb: true, meta: true, subs: false, langs: "en", over: false, num: true, speed: "", folder: "", plmode: "auto" },
  cfg: null, urls: [], preview: null, previewFor: "", selection: new Set(), analyzing: 0, searchResults: [],
};

const savePrefs = () => store("mst.prefs", { kind: S.kind, format: S.format, q: S.q, o: { ...S.o, folder: "" } });

// ───────────────────────── format / quality / options UI ─────────────────────────

function renderFormat() {
  $$("#kind-seg button").forEach((b) => b.classList.toggle("active", b.dataset.kind === S.kind));
  const host = $("#format-chips");
  host.innerHTML = FORMATS[S.kind].map((f) =>
    `<button type="button" class="chip ${f === S.format ? "active" : ""}" data-f="${f}" title="${FORMAT_HINT[f]}" aria-label="${f.toUpperCase()}" aria-pressed="${f === S.format}">${f.toUpperCase()}</button>`).join("");
}

function renderQuality() {
  const host = $("#quality-chips"), note = $("#quality-note");
  const label = $("#quality-label");
  if (S.kind === "audio") {
    label.textContent = "Bitrate";
    if (!LOSSY.has(S.format)) {
      host.innerHTML = "";
      note.textContent = S.format === "opus" ? "original stream, no re-encode" : "lossless — no bitrate to pick";
      return;
    }
    note.textContent = "";
    const list = [...S.cfg.audio_bitrates, "best"];
    host.innerHTML = list.map((q) =>
      `<button type="button" class="chip ${q === S.q.audio ? "active" : ""}" data-q="${q}">${q === "best" ? "Best" : q}${q === "best" ? "" : "<small>kbps</small>"}</button>`).join("");
    return;
  }
  label.textContent = "Resolution";
  const maxH = maxAvailableHeight();
  note.textContent = maxH ? `up to ${maxH}p for this video` : "";
  const list = [...S.cfg.video_heights, "best"];
  const eff = effectiveVideoQuality();
  host.innerHTML = list.map((q) => {
    const off = maxH && q !== "best" && Number(q) > maxH;
    const text = q === "best" ? "Best" : HEIGHT_LABEL[q] || `${q}p`;
    return `<button type="button" class="chip ${q === eff ? "active" : ""} ${off ? "is-disabled" : ""}" data-q="${q}" aria-pressed="${q === eff}" ${off ? 'aria-disabled="true" title="Not offered for this video"' : ""}>${text}</button>`;
  }).join("");
}

/** Tallest stream of the previewed single video (null when unknown / playlist). */
function maxAvailableHeight() {
  const h = S.preview?.kind === "single" ? S.preview.available_heights : null;
  return h?.length ? Math.max(...h) : null;
}

/** The resolution that will actually be fetched: the chosen one, capped by what exists. */
function effectiveVideoQuality() {
  const maxH = maxAvailableHeight();
  if (!maxH || S.q.video === "best" || Number(S.q.video) <= maxH) return S.q.video;
  const fit = S.cfg.video_heights.map(Number).filter((h) => h <= maxH);
  return String(fit.length ? Math.max(...fit) : S.q.video);
}

function renderOptions() {
  $("#o-thumb").checked = S.o.thumb;
  $("#o-meta").checked = S.o.meta;
  $("#o-subs").checked = S.o.subs && S.kind === "video";
  $("#o-over").checked = S.o.over;
  $("#o-num").checked = S.o.num;
  $("#o-langs").value = S.o.langs;
  $("#o-speed").value = S.o.speed;
  $("#o-folder").value = S.o.folder;
  $$("#pl-seg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === S.o.plmode));
  $("#opt-subs").classList.toggle("is-off", S.kind !== "video");
  $("#subs-langs").hidden = !(S.o.subs && S.kind === "video");
  const noCover = NO_COVER.has(S.format);
  $("#opt-thumb").classList.toggle("is-off", noCover);
  $("#opt-thumb-d").textContent = noCover ? `Not supported in ${S.format.toUpperCase()} files` : "Thumbnail becomes the file's artwork";
}

function setKind(kind) {
  S.kind = kind;
  if (!FORMATS[kind].includes(S.format)) S.format = FORMATS[kind][0];
  refreshAll();
}

function refreshAll() {
  renderFormat(); renderQuality(); renderOptions(); updateGo(); savePrefs();
}

// ───────────────────────── URL input + preview ─────────────────────────

function readUrls() {
  const text = $("#url-input").value;
  S.urls = extractUrls(text);
  const hint = $("#url-hint");
  const raw = text.trim();
  hint.className = "url-hint";
  if (!raw) hint.innerHTML = "";
  else if (!S.urls.length) { hint.classList.add("bad"); hint.innerHTML = `<i class="bi bi-exclamation-circle"></i>No YouTube / YouTube Music link found`; }
  else {
    const skipped = raw.split(/\s+/).filter(Boolean).length - S.urls.length;
    hint.classList.add("ok");
    hint.innerHTML = `<i class="bi bi-check2-circle"></i>${S.urls.length} link${S.urls.length > 1 ? "s" : ""} ready${skipped > 0 ? ` <span class="muted">· ${skipped} ignored</span>` : ""}`;
  }
  updateGo();
  if (S.urls.length === 1) { if (S.urls[0] !== S.previewFor) analyzeSoon(S.urls[0]); }
  else clearPreview();
}

const analyzeSoon = debounce((url) => analyze(url), 550);

function clearPreview() {
  analyzeSoon.cancel();
  S.analyzing++;  // invalidate in-flight requests
  S.preview = null; S.previewFor = ""; S.selection = new Set();
  $("#preview").innerHTML = "";
  $("#amb-banner").hidden = true;
  renderQuality(); updateGo();
}

async function analyze(url) {
  const ticket = ++S.analyzing;
  S.previewFor = url;
  $("#amb-banner").hidden = true;
  $("#preview").innerHTML = `
    <div class="preview glass-in"><div class="p-top"><div class="p-thumb skeleton"></div>
      <div style="flex:1"><div class="skeleton" style="height:16px;width:92%;margin-bottom:9px"></div><div class="skeleton" style="height:12px;width:55%"></div></div></div></div>`;
  try {
    const info = await api.info(url, S.o.plmode);
    if (ticket !== S.analyzing) return;  // a newer request superseded this one
    S.preview = info;
    S.selection = new Set(info.entries.filter((e) => e.available).map((e) => e.index));
    renderPreview();
    renderQuality();
    updateGo();
  } catch (e) {
    if (ticket !== S.analyzing) return;
    S.preview = null;
    $("#preview").innerHTML = `<div class="job-error mt-2"><i class="bi bi-exclamation-octagon-fill"></i><span>${esc(e.detail || e.message)}</span></div>`;
    updateGo();
  }
}

function renderPreview() {
  const p = S.preview;
  if (!p) return;
  const amb = $("#amb-banner");
  if (p.ambiguous) {
    const pl = p.kind === "playlist";
    amb.hidden = false;
    amb.innerHTML = `<div class="small mb-2"><i class="bi bi-info-circle-fill me-1" style="color:var(--g2)"></i>This link points to a video <b>and</b> a playlist.</div>
      <div class="seg full"><button type="button" class="${!pl ? "active" : ""}" data-pick="single">Just this video</button><button type="button" class="${pl ? "active" : ""}" data-pick="playlist">Whole playlist</button></div>`;
  } else amb.hidden = true;

  const tags = [];
  if (p.kind === "single") {
    if (p.duration) tags.push(`<span class="tag"><i class="bi bi-clock"></i>${fmtDuration(p.duration)}</span>`);
  } else tags.push(`<span class="tag"><i class="bi bi-collection-play"></i>${p.entry_count} video${p.entry_count === 1 ? "" : "s"}</span>`);
  tags.push(`<span class="tag"><i class="bi bi-folder2"></i>downloads/${esc(p.folder_preview)}</span>`);

  const el = htmlToEl(`
    <div class="preview glass-in">
      <div class="p-top">
        <img class="p-thumb" alt="" src="${esc(p.thumbnail || "")}" onerror="this.style.visibility='hidden'">
        <div style="min-width:0">
          <div class="p-title clamp-2">${esc(p.title || "(untitled)")}</div>
          ${p.uploader ? `<div class="muted" style="font-size:12.5px">${esc(p.uploader)}</div>` : ""}
          <div class="flex flex-wrap gap-1 mt-2">${tags.join("")}</div>
        </div>
      </div>
      <div class="flex gap-2 mt-2">
        <button class="btn-ghost sm flex-1" data-prev-play><i class="bi bi-play-fill"></i>Play${p.kind === "playlist" ? " playlist" : ""}</button>
        ${p.kind === "playlist" ? `<button class="btn-ghost sm flex-1" data-prev-shuffle><i class="bi bi-shuffle"></i>Shuffle playlist</button>` : ""}
      </div>
      ${p.kind === "playlist" ? `
        <div class="flex items-center justify-between mt-3">
          <label class="form-check m-0 flex items-center gap-2"><input class="form-check-input m-0" type="checkbox" id="sel-all"><span class="form-check-label">Select all</span></label>
          <span class="muted" style="font-size:12.5px" id="sel-count"></span>
        </div>
        <input id="sel-spec" class="form-control mt-2" placeholder="Pick by number: 1-5, 8, 12-" style="padding:7px 12px;font-size:13px" autocomplete="off">
        <div class="entries" id="entries">${p.entries.map(entryHTML).join("")}</div>
        ${p.entry_count >= (S.cfg?.max_playlist_items || 500) ? `<div class="faint mt-2" style="font-size:12px">Only the first ${S.cfg.max_playlist_items} tracks are listed.</div>` : ""}
      ` : ""}
    </div>`);
  $("#preview").replaceChildren(el);
  if (p.kind === "playlist") syncSelectionUI();
}

function entryHTML(e) {
  const off = !e.available;
  return `<label class="entry ${off ? "is-off" : ""}" data-i="${e.index}">
    <input class="form-check-input m-0" type="checkbox" ${off ? "disabled" : ""} data-i="${e.index}">
    <span class="e-idx mono">${e.index}</span>
    <img loading="lazy" alt="" src="${esc(e.thumbnail || "")}" onerror="this.style.visibility='hidden'">
    <span class="e-title clamp-2">${esc(e.title)}${e.uploader ? `<span class="faint"> · ${esc(e.uploader)}</span>` : ""}</span>
    <span class="e-dur mono">${e.duration ? fmtDuration(e.duration) : ""}</span>
  </label>`;
}

function syncSelectionUI({ fromSpec = false } = {}) {
  const p = S.preview;
  if (!p || p.kind !== "playlist") return;
  const avail = p.entries.filter((e) => e.available).map((e) => e.index);
  $$("#entries input[type=checkbox]").forEach((cb) => { cb.checked = S.selection.has(+cb.dataset.i); });
  const all = $("#sel-all");
  all.checked = avail.length > 0 && avail.every((i) => S.selection.has(i));
  all.indeterminate = !all.checked && S.selection.size > 0;
  $("#sel-count").textContent = `${S.selection.size} of ${avail.length} selected`;
  if (!fromSpec) $("#sel-spec").value = S.selection.size === avail.length ? "" : compressSelection([...S.selection]);
  updateGo();
}

function playPreview(shuffle) {
  const p = S.preview;
  if (!p) return;
  const tracks = p.kind === "playlist"
    ? p.entries.filter((x) => x.available && S.selection.has(x.index)).map(ytTrack)
    : [ytTrack({ video_id: p.url.split("v=")[1]?.split("&")[0], title: p.title, uploader: p.uploader, thumbnail: p.thumbnail, duration: p.duration, url: p.url })];
  if (!tracks.length) return toast("Select at least one track first.", "info");
  window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks, start: 0, shuffle } }));
}

function onPreviewEvent(e) {
  if (e.target.closest("[data-prev-play]")) return playPreview(false);
  if (e.target.closest("[data-prev-shuffle]")) return playPreview(true);
  const pick = e.target.closest("[data-pick]");
  if (pick) { S.o.plmode = pick.dataset.pick; renderOptions(); analyze(S.urls[0]); return; }
  if (e.target.id === "sel-all") {
    const avail = S.preview.entries.filter((x) => x.available).map((x) => x.index);
    S.selection = e.target.checked ? new Set(avail) : new Set();
    syncSelectionUI(); return;
  }
  const cb = e.target.closest('#entries input[type=checkbox]');
  if (cb) { const i = +cb.dataset.i; cb.checked ? S.selection.add(i) : S.selection.delete(i); syncSelectionUI(); }
}

// ───────────────────────── submit ─────────────────────────

function buildBody(urls) {
  const body = {
    urls,
    format: S.format,
    playlist_mode: S.o.plmode,
    embed_metadata: S.o.meta,
    embed_thumbnail: S.o.thumb && !NO_COVER.has(S.format),
    overwrite: S.o.over,
    numbering: S.o.num,
  };
  if (S.kind === "audio" && LOSSY.has(S.format)) body.quality = S.q.audio;
  if (S.kind === "video") {
    // a previewed single video is capped to what it really offers, so the job's badge tells the truth
    body.quality = urls.length === 1 && urls[0] === S.previewFor ? effectiveVideoQuality() : S.q.video;
    if (S.o.subs) { body.subtitles = true; body.subtitle_langs = S.o.langs.split(",").map((x) => x.trim()).filter(Boolean); }
  }
  if (S.o.speed) body.speed_limit = Number(S.o.speed);
  if (S.o.folder.trim()) body.folder = S.o.folder.trim();
  return body;
}

function selectionSpec() {
  const p = S.preview;
  if (!p || p.kind !== "playlist" || S.urls.length !== 1) return null;
  const avail = p.entries.filter((e) => e.available).length;
  return S.selection.size === avail ? null : compressSelection([...S.selection]);
}

async function submit(urls, btn) {
  const body = buildBody(urls);
  const spec = urls.length === 1 && urls[0] === S.urls[0] ? selectionSpec() : null;
  if (spec) body.playlist_items = spec;
  const res = await withBusy(btn, () => api.create(body));
  res.rejected.forEach((r) => toast(`${r.url} — ${r.error}`, "warn", { title: "Skipped" }));
  const n = res.jobs.length;
  toast(n === 1 ? (res.jobs[0].title && res.jobs[0].title !== res.jobs[0].url ? res.jobs[0].title : "Added to the queue") : `${n} downloads added`, "ok", { title: "Queued", ms: 3200 });
  window.dispatchEvent(new CustomEvent("go-queue"));
  return res;
}

async function onGo() {
  if (!S.urls.length) return;
  try {
    await submit(S.urls, $("#go-btn"));
    $("#url-input").value = "";
    S.urls = []; S.o.folder = ""; $("#o-folder").value = "";
    clearPreview(); readUrls();
  } catch (e) { toast(e.detail || e.message, "err", { title: "Could not queue" }); }
}

function updateGo() {
  const btn = $("#go-btn"), label = $("#go-label"), sub = $("#go-sub");
  const n = S.urls.length;
  const sel = S.preview?.kind === "playlist" && n === 1 ? S.selection.size : null;
  btn.disabled = n === 0 || sel === 0;
  label.textContent = sel != null ? (sel === 0 ? "Select at least one track" : `Download ${sel} track${sel === 1 ? "" : "s"}`) : n > 1 ? `Download ${n} links` : "Download";
  const vq = effectiveVideoQuality();
  const q = S.kind === "audio" ? (LOSSY.has(S.format) ? (S.q.audio === "best" ? "best" : `${S.q.audio} kbps`) : "") : (vq === "best" ? "best" : HEIGHT_LABEL[vq] || `${vq}p`);
  sub.textContent = `${S.format.toUpperCase()}${q ? " · " + q : ""}${S.preview ? ` → downloads/${S.preview.folder_preview}` : ""}`;
}

// ───────────────────────── search ─────────────────────────

async function doSearch(e) {
  e.preventDefault();
  const q = $("#search-input").value.trim();
  if (q.length < 2) return;
  const host = $("#search-results");
  host.innerHTML = Array.from({ length: 4 }, () => `<div class="result"><div class="skeleton" style="width:92px;height:52px"></div><div style="flex:1"><div class="skeleton" style="height:13px;width:80%;margin-bottom:7px"></div><div class="skeleton" style="height:11px;width:40%"></div></div></div>`).join("");
  try {
    S.searchResults = await api.search(q, $("#search-music").checked);
    if (!S.searchResults.length) { host.innerHTML = `<div class="muted text-center p-3">No results.</div>`; return; }
    host.innerHTML = S.searchResults.map((r, i) => `
      <div class="result" data-key="yt:${esc(r.video_id)}" style="animation-delay:${i * 35}ms">
        <img loading="lazy" alt="" src="${esc(r.thumbnail || "")}" onerror="this.style.visibility='hidden'">
        <div style="flex:1;min-width:0">
          <div class="r-title clamp-2">${esc(r.title)}</div>
          <div class="muted" style="font-size:12px">${esc(r.uploader || "")}${r.duration ? ` · ${fmtDuration(r.duration)}` : ""}</div>
        </div>
        <button class="btn-icon info" data-play="${i}" aria-label="Play preview" title="Play preview"><i class="bi bi-play-fill"></i></button>
        <button class="btn-icon ok" data-dl="${i}" aria-label="Download" title="Download"><i class="bi bi-download"></i></button>
      </div>`).join("") + `<div class="flex gap-2 mt-1">
        <button class="btn-ghost flex-1" data-play-all><i class="bi bi-play-circle"></i>Play all</button>
        <button class="btn-ghost flex-1" data-shuffle-all><i class="bi bi-shuffle"></i>Shuffle</button>
        <button class="btn-ghost flex-1" data-dl-all><i class="bi bi-cloud-download"></i>Download all ${S.searchResults.length}</button></div>`;
  } catch (err) { host.innerHTML = `<div class="job-error"><i class="bi bi-exclamation-octagon-fill"></i><span>${esc(err.detail || err.message)}</span></div>`; }
}

async function onResultClick(e) {
  const play = e.target.closest("[data-play]");
  const playAll = e.target.closest("[data-play-all]");
  if (e.target.closest("[data-shuffle-all]")) { window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: S.searchResults.map(ytTrack), shuffle: true } })); return; }
  if (play || playAll) {
    const tracks = S.searchResults.map(ytTrack);
    window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks, start: play ? Number(play.dataset.play) : 0 } }));
    return;
  }
  const one = e.target.closest("[data-dl]");
  const all = e.target.closest("[data-dl-all]");
  if (!one && !all) return;
  const btn = one || all;
  try {
    const urls = one ? [S.searchResults[+one.dataset.dl].url] : S.searchResults.map((r) => r.url);
    await submit(urls, btn);
  } catch (err) { toast(err.detail || err.message, "err"); }
}

/** Fill the "Folder name" suggestions with the folders that already exist. */
export async function refreshFolders() {
  try {
    const list = await api.libFolders();
    $("#folder-list").innerHTML = list.map((f) => `<option value="${esc(f)}"></option>`).join("");
  } catch { /* suggestions are a convenience */ }
}

// ───────────────────────── init ─────────────────────────

function setSource(src) {
  S.src = src;
  $$("#src-seg button").forEach((b) => b.classList.toggle("active", b.dataset.src === src));
  $("#src-link").hidden = src !== "link";
  $("#src-search").hidden = src !== "search";
  if (src === "search") setTimeout(() => $("#search-input").focus(), 50);
  else $("#url-input").focus();
}

export function focusInput() { setSource("link"); $("#url-input").focus(); }

export function initComposer(cfg) {
  S.cfg = cfg;
  S.o.thumb = cfg.embed_thumbnail; S.o.meta = cfg.embed_metadata; S.o.num = cfg.playlist_numbering !== false;
  const fmt = cfg.default_format;
  S.kind = cfg.audio_formats.includes(fmt) ? "audio" : "video"; S.format = fmt;
  S.q = { audio: cfg.default_audio_quality, video: cfg.default_video_quality };
  const saved = store("mst.prefs");
  if (saved) {
    if (FORMATS[saved.kind]?.includes(saved.format)) { S.kind = saved.kind; S.format = saved.format; }
    Object.assign(S.q, saved.q || {});
    Object.assign(S.o, saved.o || {}, { folder: "" });
  }

  renderFormat(); renderQuality(); renderOptions(); updateGo();

  $("#src-seg").addEventListener("click", (e) => { const b = e.target.closest("[data-src]"); if (b) setSource(b.dataset.src); });
  $("#kind-seg").addEventListener("click", (e) => { const b = e.target.closest("[data-kind]"); if (b) setKind(b.dataset.kind); });
  $("#format-chips").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (b) { S.format = b.dataset.f; refreshAll(); } });
  $("#quality-chips").addEventListener("click", (e) => {
    const b = e.target.closest("[data-q]");
    if (!b || b.classList.contains("is-disabled")) return;
    S.q[S.kind] = b.dataset.q; refreshAll();
  });
  $("#pl-seg").addEventListener("click", (e) => {
    const b = e.target.closest("[data-mode]");
    if (!b) return;
    S.o.plmode = b.dataset.mode; renderOptions(); savePrefs();
    if (S.urls.length === 1) analyze(S.urls[0]);
  });

  // switches/selects re-render dependent UI; free-text inputs only store (re-rendering would fight the caret)
  const bind = (id, key, prop = "checked", rerender = true) => $(id).addEventListener("input", (e) => {
    S.o[key] = e.target[prop];
    if (rerender) renderOptions();
    savePrefs();
  });
  bind("#o-thumb", "thumb"); bind("#o-meta", "meta"); bind("#o-subs", "subs"); bind("#o-over", "over"); bind("#o-num", "num");
  bind("#o-speed", "speed", "value");
  bind("#o-langs", "langs", "value", false); bind("#o-folder", "folder", "value", false);

  $("#adv").addEventListener("show.bs.collapse", () => { $("#adv-chev").style.transform = "rotate(180deg)"; });
  $("#adv").addEventListener("hide.bs.collapse", () => { $("#adv-chev").style.transform = ""; });

  const input = $("#url-input");
  input.addEventListener("input", readUrls);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); onGo(); } });
  $("#paste-btn").addEventListener("click", async () => {
    try {
      const t = await navigator.clipboard.readText();
      if (t) { input.value = input.value ? input.value.trimEnd() + "\n" + t : t; readUrls(); }
    } catch { toast("Clipboard access was blocked — paste with Ctrl+V instead.", "warn"); }
  });
  $("#preview").addEventListener("change", onPreviewEvent);
  $("#preview").addEventListener("click", (e) => { if (e.target.closest("[data-pick], [data-prev-play], [data-prev-shuffle]")) onPreviewEvent(e); });
  $("#amb-banner").addEventListener("click", (e) => { if (e.target.closest("[data-pick]")) onPreviewEvent(e); });
  $("#preview").addEventListener("input", (e) => {
    if (e.target.id !== "sel-spec" || !S.preview) return;
    const total = S.preview.entries.length;
    const sel = parseSelection(e.target.value, total);
    e.target.style.borderColor = sel ? "" : "rgba(251,113,133,.7)";
    if (sel) { S.selection = new Set([...sel].filter((i) => S.preview.entries[i - 1]?.available)); syncSelectionUI({ fromSpec: true }); }
  });

  $("#go-btn").addEventListener("click", onGo);
  $("#search-form").addEventListener("submit", doSearch);
  $("#search-results").addEventListener("click", onResultClick);
  window.addEventListener("player-change", (e) => {
    document.querySelectorAll("#search-results .result").forEach((el) => el.classList.toggle("is-playing", !!e.detail.key && el.dataset.key === e.detail.key));
  });
  // "Download this" button on the mini player (YouTube tracks)
  window.addEventListener("queue-download", async (e) => {
    try { await submit(e.detail.urls, null); } catch (err) { toast(err.detail || err.message, "err"); }
  });
  refreshFolders();
  window.addEventListener("folders-changed", refreshFolders);

  // paste anywhere → straight into the box
  window.addEventListener("paste", (e) => {
    const t = e.target;
    if (t.matches?.("input, textarea, select, [contenteditable]")) return;
    const text = e.clipboardData?.getData("text");
    if (text && extractUrls(text).length) { setSource("link"); input.value = text; readUrls(); window.dispatchEvent(new CustomEvent("go-new")); }
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "/" && !e.target.matches?.("input, textarea, select, [contenteditable]") && !e.ctrlKey && !e.metaKey) { e.preventDefault(); window.dispatchEvent(new CustomEvent("go-new")); focusInput(); }
  });
}
