// "New download" card: link / search input, format + quality pickers, option
// switches, URL preview (with playlist track selection) and submission.

import { api } from "./api.js";
import { ytTrack } from "./player.js";
import { toast, withBusy } from "./ui.js";
import {
  $, $$, esc, htmlToEl, debounce, extractUrls, fmtDuration, compressSelection, parseSelection, store,
  maybeCookieBanner, showCookieBanner, hideCookieBanner,
} from "./util.js";

const FORMATS = { audio: ["mp3", "m4a", "opus", "flac", "wav"], video: ["mp4", "mkv", "webm"] };
const FORMAT_HINT = { mp3: "most compatible", m4a: "AAC, small & clean", opus: "original, best size/quality", flac: "lossless", wav: "uncompressed", mp4: "H.264, plays everywhere", mkv: "any codec, flexible", webm: "VP9 / Opus" };
const LOSSY = new Set(["mp3", "m4a"]);
const NO_COVER = new Set(["wav", "webm"]); // yt-dlp cannot embed artwork into these
const HEIGHT_LABEL = { 1440: "1440p", 2160: "4K" };

const S = {
  src: "link", kind: "audio", format: "mp3", q: { audio: "192", video: "1080" },
  o: { thumb: true, meta: true, subs: false, langs: "en", over: false, sb: false, num: true, speed: "", folder: "", plmode: "auto" },
  cfg: null, urls: [], preview: null, previewFor: "", selection: new Set(), analyzing: 0, searchResults: [], feed: [], playlists: [], accLoaded: false,
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
  $("#o-sb").checked = S.o.sb;
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
  renderUrlChips();
  if (S.urls.length === 1) { if (S.urls[0] !== S.previewFor) analyzeSoon(S.urls[0]); }
  else if (S.urls.length > 1) {
    const keep = S.urls.includes(S.previewFor) ? S.previewFor : S.urls[0];
    if (keep !== S.previewFor) analyzeSoon(keep);
  } else clearPreview();
}

function renderUrlChips() {
  const host = $("#url-chips");
  if (!host) return;
  if (S.urls.length < 2) { host.hidden = true; host.innerHTML = ""; return; }
  host.hidden = false;
  host.innerHTML = S.urls.map((u, i) => {
    const short = u.replace(/^https?:\/\/(www\.)?/, "").slice(0, 42);
    const active = u === S.previewFor ? " active" : "";
    return `<button type="button" class="url-chip${active}" data-url-i="${i}" title="${esc(u)}"><span class="uc-t">${esc(short)}</span><span class="uc-x" data-url-rm="${i}" aria-label="Remove">×</span></button>`;
  }).join("");
}

const analyzeSoon = debounce((url) => analyze(url), 550);

function clearPreview() {
  analyzeSoon.cancel();
  S.analyzing++;  // invalidate in-flight requests
  S.preview = null; S.previewFor = ""; S.selection = new Set();
  $("#preview").innerHTML = "";
  $("#amb-banner").hidden = true;
  renderUrlChips();
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
    renderUrlChips();
    renderPreview();
    renderQuality();
    updateGo();
  } catch (e) {
    if (ticket !== S.analyzing) return;
    S.preview = null;
    const msg = e.detail || e.message;
    maybeCookieBanner(msg);
    $("#preview").innerHTML = `<div class="job-error mt-2"><i class="bi bi-exclamation-octagon-fill"></i><span>${esc(msg)}</span></div>`;
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
  tags.push(`<span class="tag local-only"><i class="bi bi-folder2"></i>downloads/${esc(p.folder_preview)}</span>`);

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
        <button class="btn-ghost sm flex-1" data-prev-cart><i class="bi bi-bag-plus"></i>${p.kind === "playlist" ? "Selected to cart" : "Add to cart"}</button>
        ${p.kind === "playlist" ? `<button class="btn-ghost sm flex-1" data-prev-sub data-bs-toggle="tooltip" title="Download new uploads automatically"><i class="bi bi-bell-fill"></i>Subscribe</button>` : ""}
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
    ${off ? "" : `<span class="e-act">
      <button type="button" class="btn-icon sm info" data-e-play="${e.index}" aria-label="Play" title="Play"><i class="bi bi-play-fill"></i></button>
      <button type="button" class="btn-icon sm" data-e-cart="${e.index}" aria-label="Add to cart" title="Add to cart"><i class="bi bi-bag-plus"></i></button>
      <button type="button" class="btn-icon sm ok" data-e-dl="${e.index}" aria-label="Download" title="Download"><i class="bi bi-download"></i></button>
    </span>`}
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

/** Cart entries for the preview: the video itself, or the selected tracks of a playlist. */
function previewEntries() {
  const p = S.preview;
  if (!p) return [];
  if (p.kind === "playlist") return p.entries.filter((x) => x.available && S.selection.has(x.index));
  return [{ url: p.url, video_id: videoIdOf(p.url), title: p.title, uploader: p.uploader, thumbnail: p.thumbnail, duration: p.duration }];
}

const videoIdOf = (url) => (String(url).match(/(?:[?&]v=|youtu\.be\/|\/shorts\/)([\w-]{11})/) || [])[1] || null;
const toCart = (entries) => window.dispatchEvent(new CustomEvent("cart-add", { detail: { entries } }));

/** One track of the previewed playlist: play it (the playlist keeps playing after it), add to cart, or download just it. */
async function onEntryAction(e) {
  const btn = e.target.closest("[data-e-play], [data-e-cart], [data-e-dl]");
  const idx = Number(btn.dataset.ePlay || btn.dataset.eCart || btn.dataset.eDl);
  const entry = S.preview?.entries.find((x) => x.index === idx);
  if (!entry) return;
  e.preventDefault();  // the row is a <label>: do not toggle its checkbox
  if (btn.dataset.ePlay) {
    const avail = S.preview.entries.filter((x) => x.available);
    window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: avail.map(ytTrack), start: avail.indexOf(entry) } }));
  } else if (btn.dataset.eCart) {
    toCart([entry]); btn.classList.add("added");
  } else {
    try { await submit([entry.url], btn); } catch (err) { toast(err.detail || err.message, "err"); }
  }
}

function onPreviewEvent(e) {
  if (e.target.closest("[data-e-play], [data-e-cart], [data-e-dl]")) return onEntryAction(e);
  if (e.target.closest("[data-prev-play]")) return playPreview(false);
  if (e.target.closest("[data-prev-cart]")) { const list = previewEntries(); return list.length ? toCart(list) : toast("Select at least one track first.", "info"); }
  if (e.target.closest("[data-prev-shuffle]")) return playPreview(true);
  if (e.target.closest("[data-prev-sub]")) {  // follow the previewed channel / playlist (subscriptions.js listens)
    window.dispatchEvent(new CustomEvent("subscribe-add", { detail: { url: S.preview.url, title: S.preview.title } }));
    return;
  }
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
    sponsorblock: S.o.sb,
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

/** The current format / quality / switches, for other submitters (the download cart). Folder excluded: the cart names its own. */
export function currentOptions() {
  const { urls, playlist_mode, folder, ...opts } = buildBody([]);  // eslint-disable-line no-unused-vars
  return opts;
}

/** Snapshot of composer format state so the cart can seed its own picker. */
export function formatState() {
  return { kind: S.kind, format: S.format, q: { ...S.q }, audio_formats: FORMATS.audio, video_formats: FORMATS.video,
    audio_bitrates: S.cfg?.audio_bitrates || ["64", "128", "192", "256", "320"],
    video_heights: S.cfg?.video_heights || ["144", "240", "360", "480", "720", "1080", "1440", "2160"] };
}

/** Apply format/quality back into the composer (keeps prefs in sync when the cart changes them). */
export function applyFormatState({ kind, format, q } = {}) {
  if (kind && FORMATS[kind]) S.kind = kind;
  if (format && FORMATS[S.kind]?.includes(format)) S.format = format;
  if (q) Object.assign(S.q, q);
  refreshAll();
}

/** "MP3 · 192 kbps" — what the current choice produces. */
export function formatLabel(state = null) {
  const kind = state?.kind || S.kind;
  const format = state?.format || S.format;
  const qq = state?.q || S.q;
  const vq = kind === "video" ? (qq.video || "best") : null;
  const q = kind === "audio" ? (LOSSY.has(format) ? (qq.audio === "best" ? "best" : `${qq.audio} kbps`) : "") : (vq === "best" ? "best" : HEIGHT_LABEL[vq] || `${vq}p`);
  return `${format.toUpperCase()}${q ? " · " + q : ""}`;
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

async function onCartAdd() {
  if (!S.urls.length) return;
  if (S.urls.length === 1 && S.preview && S.previewFor === S.urls[0]) {
    const list = previewEntries();
    return list.length ? toCart(list) : toast("Select at least one track first.", "info");
  }
  // several links: read each (in order) so the cart shows titles; playlists contribute their available tracks
  await withBusy($("#cart-add-btn"), async () => {
    const entries = [];
    for (const url of S.urls.slice(0, 25)) {
      try {
        const info = await api.info(url, S.o.plmode);
        if (info.kind === "playlist") entries.push(...info.entries.filter((x) => x.available));
        else entries.push({ url: info.url, video_id: videoIdOf(info.url), title: info.title, uploader: info.uploader, thumbnail: info.thumbnail, duration: info.duration });
      } catch (e) { toast(`${url} — ${e.detail || e.message}`, "warn", { title: "Skipped" }); }
    }
    if (S.urls.length > 25) toast("Only the first 25 links were read.", "info");
    toCart(entries);
  });
}

function updateGo() {
  const btn = $("#go-btn"), label = $("#go-label"), sub = $("#go-sub");
  const n = S.urls.length;
  const sel = S.preview?.kind === "playlist" && n === 1 ? S.selection.size : null;
  btn.disabled = n === 0 || sel === 0;
  $("#cart-add-btn").disabled = n === 0 || sel === 0;
  label.textContent = sel != null ? (sel === 0 ? "Select at least one track" : `Download ${sel} track${sel === 1 ? "" : "s"}`) : n > 1 ? `Download ${n} links` : "Download";
  const vq = effectiveVideoQuality();
  const q = S.kind === "audio" ? (LOSSY.has(S.format) ? (S.q.audio === "best" ? "best" : `${S.q.audio} kbps`) : "") : (vq === "best" ? "best" : HEIGHT_LABEL[vq] || `${vq}p`);
  const dest = S.cfg?.server_mode ? " → saved to your device" : S.preview ? ` → downloads/${S.preview.folder_preview}` : "";
  sub.textContent = `${S.format.toUpperCase()}${q ? " · " + q : ""}${dest}`;
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
    host.innerHTML = resultsHTML(S.searchResults);
  } catch (err) {
    const msg = err.detail || err.message;
    maybeCookieBanner(msg);
    host.innerHTML = `<div class="job-error"><i class="bi bi-exclamation-octagon-fill"></i><span>${esc(msg)}</span></div>`;
  }
}

/** Track rows + the "play all / shuffle / cart / download all" bar — shared by search results and the home feed. */
function resultsHTML(list) {
  return list.map((r, i) => `
      <div class="result" data-key="yt:${esc(r.video_id)}" style="animation-delay:${Math.min(i, 14) * 35}ms">
        <img loading="lazy" alt="" src="${esc(r.thumbnail || "")}" onerror="this.style.visibility='hidden'">
        <div style="flex:1;min-width:0">
          <div class="r-title clamp-2">${esc(r.title)}</div>
          <div class="muted" style="font-size:12px">${esc(r.uploader || "")}${r.duration ? ` · ${fmtDuration(r.duration)}` : ""}</div>
        </div>
        <button class="btn-icon info" data-play="${i}" aria-label="Play preview" title="Play preview"><i class="bi bi-play-fill"></i></button>
        <button class="btn-icon" data-cart="${i}" aria-label="Add to cart" title="Add to cart"><i class="bi bi-bag-plus"></i></button>
        <button class="btn-icon ok" data-dl="${i}" aria-label="Download" title="Download"><i class="bi bi-download"></i></button>
      </div>`).join("") + `<div class="flex gap-2 mt-1">
        <button class="btn-ghost flex-1" data-play-all><i class="bi bi-play-circle"></i>Play all</button>
        <button class="btn-ghost flex-1" data-shuffle-all><i class="bi bi-shuffle"></i>Shuffle</button>
        <button class="btn-ghost flex-1" data-cart-all><i class="bi bi-bag-plus"></i>All to cart</button>
        <button class="btn-ghost flex-1" data-dl-all><i class="bi bi-cloud-download"></i>Download all ${list.length}</button></div>`;
}

/** Click handler for a ``.results`` list; ``list()`` returns the entries it currently shows. */
async function onResultClick(e, list = () => S.searchResults) {
  const items = list();
  const play = e.target.closest("[data-play]");
  const playAll = e.target.closest("[data-play-all]");
  if (e.target.closest("[data-shuffle-all]")) { window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: items.map(ytTrack), shuffle: true } })); return; }
  if (play || playAll) {
    const tracks = items.map(ytTrack);
    window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks, start: play ? Number(play.dataset.play) : 0 } }));
    return;
  }
  const cartOne = e.target.closest("[data-cart]");
  if (cartOne) { toCart([items[+cartOne.dataset.cart]]); cartOne.classList.add("added"); return; }
  if (e.target.closest("[data-cart-all]")) { toCart(items); $$("[data-cart]", e.currentTarget).forEach((b) => b.classList.add("added")); return; }
  const one = e.target.closest("[data-dl]");
  const all = e.target.closest("[data-dl-all]");
  if (!one && !all) return;
  const btn = one || all;
  try {
    const urls = one ? [items[+one.dataset.dl].url] : items.map((r) => r.url);
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

// ───────────────────────── my YouTube (signed-in account) ─────────────────────────

const accMsg = (html) => { $("#acc-msg").innerHTML = html; $("#acc-body").hidden = true; };

function accSkeleton() {
  $("#acc-playlists").innerHTML = Array.from({ length: 4 }, () => `<div class="pl-card"><div class="pl-art skeleton"></div><div class="skeleton" style="height:12px;width:80%"></div></div>`).join("");
  $("#acc-feed").innerHTML = Array.from({ length: 3 }, () => `<div class="result"><div class="skeleton" style="width:92px;height:52px"></div><div style="flex:1"><div class="skeleton" style="height:13px;width:80%;margin-bottom:7px"></div><div class="skeleton" style="height:11px;width:40%"></div></div></div>`).join("");
}

const needCookies = (text) => {
  showCookieBanner(text || "Add your YouTube cookies to see playlists and your home feed.");
  return `<div class="glass-in p-3 text-center">
  <div class="mb-2"><i class="bi bi-person-lock" style="font-size:26px;color:var(--g2)"></i></div>
  <div class="small mb-2">${esc(text)}</div>
  <button class="btn-grad sm" type="button" data-acc-cookies><i class="bi bi-key-fill me-1"></i>Add YouTube cookies</button></div>`;
};

function playlistCard(p, i) {
  const icon = p.music ? "heart-fill" : p.id === "WL" ? "clock-history" : p.pinned ? "hand-thumbs-up-fill" : "music-note-list";
  const art = p.thumbnail ? `<img loading="lazy" alt="" src="${esc(p.thumbnail)}" onerror="this.remove()">` : `<i class="bi bi-${icon}"></i>`;
  const sub = [p.count ? `${p.count} videos` : "", p.music ? "YouTube Music" : p.uploader || ""].filter(Boolean).join(" · ");
  return `<button class="pl-card" type="button" data-pl="${i}" style="animation-delay:${Math.min(i, 12) * 30}ms" title="${esc(p.title)}">
    <div class="pl-art">${art}</div><div class="pl-t clamp-2">${esc(p.title)}</div>${sub ? `<div class="pl-s clamp-2">${esc(sub)}</div>` : ""}</button>`;
}

async function loadAccount(force = false) {
  if (S.accLoaded && !force) return;
  S.accLoaded = true;
  $("#acc-msg").innerHTML = "";
  $("#acc-body").hidden = false;
  accSkeleton();
  const [pl, home] = await Promise.allSettled([api.accountPlaylists(), api.accountHome()]);
  if (pl.status === "rejected") { S.accLoaded = false; return accMsg(`<div class="job-error"><i class="bi bi-exclamation-octagon-fill"></i><span>${esc(pl.reason.detail || pl.reason.message)}</span></div>`); }
  if (!pl.value.signed_in) { S.accLoaded = false; return accMsg(needCookies(pl.value.message)); }

  S.playlists = pl.value.playlists;
  $("#acc-playlists").innerHTML = S.playlists.map(playlistCard).join("");
  S.feed = home.status === "fulfilled" ? home.value.entries : [];
  const warn = pl.value.message || (home.status === "fulfilled" ? home.value.message : home.reason.detail || home.reason.message);
  if (warn && !S.feed.length) {
    maybeCookieBanner(warn);
    $("#acc-msg").innerHTML = `<div class="job-error mb-2"><i class="bi bi-exclamation-triangle-fill"></i><span>${esc(warn)}</span></div>`;
  }
  $("#acc-feed").innerHTML = S.feed.length ? resultsHTML(S.feed) : `<div class="muted text-center p-3">Nothing to show in your home feed.</div>`;
}

/** Open one of the account's playlists in the normal link flow (track picker, play, shuffle, cart, download). */
function openAccountPlaylist(i) {
  const p = S.playlists[i];
  if (!p) return;
  setSource("link");
  $("#url-input").value = p.url;
  readUrls();
  $("#preview").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ───────────────────────── init ─────────────────────────

function setSource(src) {
  S.src = src;
  $$("#src-seg button").forEach((b) => b.classList.toggle("active", b.dataset.src === src));
  $("#src-link").hidden = src !== "link";
  $("#src-search").hidden = src !== "search";
  $("#src-account").hidden = src !== "account";
  if (src === "account") loadAccount();
  else if (src === "search") setTimeout(() => $("#search-input").focus(), 50);
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
  bind("#o-thumb", "thumb"); bind("#o-meta", "meta"); bind("#o-subs", "subs"); bind("#o-over", "over"); bind("#o-sb", "sb"); bind("#o-num", "num");
  bind("#o-speed", "speed", "value");
  bind("#o-langs", "langs", "value", false); bind("#o-folder", "folder", "value", false);

  $("#adv").addEventListener("show.bs.collapse", () => { $("#adv-chev").style.transform = "rotate(180deg)"; });
  $("#adv").addEventListener("hide.bs.collapse", () => { $("#adv-chev").style.transform = ""; });

  const input = $("#url-input");
  input.addEventListener("input", readUrls);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); onGo(); } });
  $("#url-chips")?.addEventListener("click", (e) => {
    const rm = e.target.closest("[data-url-rm]");
    if (rm) {
      e.preventDefault(); e.stopPropagation();
      const i = Number(rm.dataset.urlRm);
      const next = S.urls.filter((_, j) => j !== i);
      input.value = next.join("\n");
      readUrls();
      return;
    }
    const chip = e.target.closest("[data-url-i]");
    if (!chip) return;
    const url = S.urls[Number(chip.dataset.urlI)];
    if (url) analyze(url);
  });
  $("#paste-btn").addEventListener("click", async () => {
    try {
      const t = await navigator.clipboard.readText();
      if (t) { input.value = input.value ? input.value.trimEnd() + "\n" + t : t; readUrls(); }
    } catch { toast("Clipboard access was blocked — paste with Ctrl+V instead.", "warn"); }
  });
  $("#preview").addEventListener("change", onPreviewEvent);
  $("#preview").addEventListener("click", (e) => { if (e.target.closest("[data-pick], [data-prev-play], [data-prev-shuffle], [data-prev-cart], [data-prev-sub], [data-e-play], [data-e-cart], [data-e-dl]")) onPreviewEvent(e); });
  $("#amb-banner").addEventListener("click", (e) => { if (e.target.closest("[data-pick]")) onPreviewEvent(e); });
  $("#preview").addEventListener("input", (e) => {
    if (e.target.id !== "sel-spec" || !S.preview) return;
    const total = S.preview.entries.length;
    const sel = parseSelection(e.target.value, total);
    e.target.style.borderColor = sel ? "" : "rgba(251,113,133,.7)";
    if (sel) { S.selection = new Set([...sel].filter((i) => S.preview.entries[i - 1]?.available)); syncSelectionUI({ fromSpec: true }); }
  });

  $("#go-btn").addEventListener("click", onGo);
  $("#cart-add-btn").addEventListener("click", onCartAdd);
  $("#search-form").addEventListener("submit", doSearch);
  $("#search-results").addEventListener("click", (e) => onResultClick(e, () => S.searchResults));
  $("#acc-feed").addEventListener("click", (e) => onResultClick(e, () => S.feed));
  $("#acc-playlists").addEventListener("click", (e) => { const c = e.target.closest("[data-pl]"); if (c) openAccountPlaylist(+c.dataset.pl); });
  $("#acc-msg").addEventListener("click", (e) => { if (e.target.closest("[data-acc-cookies]")) window.dispatchEvent(new CustomEvent("open-cookies")); });
  $("#acc-refresh").addEventListener("click", () => loadAccount(true));
  window.addEventListener("cookies-changed", () => { S.accLoaded = false; if (S.src === "account") loadAccount(true); });
  window.addEventListener("player-change", (e) => {
    document.querySelectorAll("#search-results .result, #acc-feed .result").forEach((el) => el.classList.toggle("is-playing", !!e.detail.key && el.dataset.key === e.detail.key));
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
