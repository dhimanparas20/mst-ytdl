// "New download" card: link / search / Mine, URL preview (playlist track picker) and submission.
// Format, quality and options live in Settings (localStorage ``mst.prefs``); home shows a summary chip.

import { api } from "./api.js";
import { navigateTo, srcFromTab, tabFromSrc } from "./router.js";
import { ytTrack } from "./player.js";
import { getUiPrefs, setUiPrefs } from "./prefs.js";
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
  src: "link",
  // Separate saved defaults for each type — download time asks which to use.
  audio: { format: "mp3", q: "192" },
  video: { format: "mp4", q: "1080" },
  o: { thumb: true, meta: true, subs: false, langs: "en", over: false, sb: false, num: true, speed: "", folder: "", plmode: "auto" },
  cfg: null, urls: [], preview: null, previewFor: "", selection: new Set(), analyzing: 0, searchResults: [], feed: [], playlists: [], accLoaded: false,
};

const savePrefs = () => store("mst.prefs", {
  audio: { ...S.audio }, video: { ...S.video }, o: { ...S.o, folder: "" },
});

// ───────────────────────── format / quality / options UI (Settings) ─────────────────────────

function renderAudioPrefs() {
  const host = $("#audio-format-chips");
  if (!host) return;
  host.innerHTML = FORMATS.audio.map((f) =>
    `<button type="button" class="chip ${f === S.audio.format ? "active" : ""}" data-af="${f}" title="${FORMAT_HINT[f]}" aria-pressed="${f === S.audio.format}">${f.toUpperCase()}</button>`).join("");
  const qHost = $("#audio-quality-chips"), note = $("#audio-quality-note");
  if (!LOSSY.has(S.audio.format)) {
    qHost.innerHTML = "";
    note.textContent = S.audio.format === "opus" ? "original stream, no re-encode" : "lossless — no bitrate to pick";
    return;
  }
  note.textContent = "";
  const list = [...(S.cfg?.audio_bitrates || ["64", "128", "192", "256", "320"]), "best"];
  qHost.innerHTML = list.map((q) =>
    `<button type="button" class="chip ${q === S.audio.q ? "active" : ""}" data-aq="${q}">${q === "best" ? "Best" : q}${q === "best" ? "" : "<small>kbps</small>"}</button>`).join("");
}

function renderVideoPrefs() {
  const host = $("#video-format-chips");
  if (!host) return;
  host.innerHTML = FORMATS.video.map((f) =>
    `<button type="button" class="chip ${f === S.video.format ? "active" : ""}" data-vf="${f}" title="${FORMAT_HINT[f]}" aria-pressed="${f === S.video.format}">${f.toUpperCase()}</button>`).join("");
  const qHost = $("#video-quality-chips"), note = $("#video-quality-note");
  note.textContent = "";
  const list = [...(S.cfg?.video_heights || ["360", "720", "1080", "1440", "2160"]), "best"];
  qHost.innerHTML = list.map((q) => {
    const text = q === "best" ? "Best" : HEIGHT_LABEL[q] || `${q}p`;
    return `<button type="button" class="chip ${String(q) === String(S.video.q) ? "active" : ""}" data-vq="${q}" aria-pressed="${String(q) === String(S.video.q)}">${text}</button>`;
  }).join("");
}

/** Tallest stream of the previewed single video (null when unknown / playlist). */
function maxAvailableHeight() {
  const h = S.preview?.kind === "single" ? S.preview.available_heights : null;
  return h?.length ? Math.max(...h) : null;
}

/** Cap chosen video height by what the previewed single actually offers. */
function effectiveVideoQuality() {
  const maxH = maxAvailableHeight();
  const want = S.video.q;
  if (!maxH || want === "best" || Number(want) <= maxH) return want;
  const fit = (S.cfg?.video_heights || []).map(Number).filter((h) => h <= maxH);
  return String(fit.length ? Math.max(...fit) : want);
}

function renderOptions() {
  if (!$("#o-thumb")) return;
  $("#o-thumb").checked = S.o.thumb;
  $("#o-meta").checked = S.o.meta;
  $("#o-subs").checked = S.o.subs;
  $("#o-over").checked = S.o.over;
  $("#o-sb").checked = S.o.sb;
  $("#o-num").checked = S.o.num;
  $("#o-langs").value = S.o.langs;
  $("#o-speed").value = S.o.speed;
  $("#o-folder").value = S.o.folder;
  $$("#pl-seg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === S.o.plmode));
  $("#subs-langs").hidden = !S.o.subs;
  const noCover = NO_COVER.has(S.audio.format);
  $("#opt-thumb").classList.toggle("is-off", noCover);
  $("#opt-thumb-d").textContent = noCover ? `Not supported in ${S.audio.format.toUpperCase()} files` : "Thumbnail becomes the file's artwork (audio)";
}

function refreshAll() {
  renderAudioPrefs(); renderVideoPrefs(); renderOptions(); updateGo(); savePrefs();
}

/** Ask Audio vs Video, then apply that type's saved defaults. Resolves to kind or null. */
export function pickDownloadKind({ title } = {}) {
  return new Promise((resolve) => {
    const modalEl = $("#dl-kind-modal");
    if (!modalEl) return resolve("audio");
    if (title) $("#dl-kind-body").textContent = title;
    else $("#dl-kind-body").textContent = "Choose audio or video — your saved defaults for that type will be used.";
    $("#dl-kind-audio-sum").textContent = kindLabel("audio");
    $("#dl-kind-video-sum").textContent = kindLabel("video");
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    let answer = null;
    const onClick = (e) => {
      const b = e.target.closest("[data-dl-kind]");
      if (!b) return;
      answer = b.dataset.dlKind;
      modal.hide();
    };
    const onHidden = () => {
      modalEl.removeEventListener("click", onClick);
      modalEl.removeEventListener("hidden.bs.modal", onHidden);
      resolve(answer);
    };
    modalEl.addEventListener("click", onClick);
    modalEl.addEventListener("hidden.bs.modal", onHidden);
    modal.show();
  });
}

function kindLabel(kind) {
  if (kind === "audio") {
    const f = S.audio.format;
    const q = LOSSY.has(f) ? (S.audio.q === "best" ? "best" : `${S.audio.q} kbps`) : "";
    return `${f.toUpperCase()}${q ? " · " + q : ""}`;
  }
  const f = S.video.format;
  const q = S.video.q === "best" ? "best" : HEIGHT_LABEL[S.video.q] || `${S.video.q}p`;
  return `${f.toUpperCase()} · ${q}`;
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
  renderVideoPrefs(); updateGo();
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
    renderVideoPrefs();
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
        <button class="btn-ghost sm flex-1" data-prev-cart><i class="bi bi-cart-plus"></i>${p.kind === "playlist" ? "Selected to cart" : "Add to cart"}</button>
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
    <img class="e-play" loading="lazy" alt="" src="${esc(e.thumbnail || "")}" data-e-play="${e.index}" onerror="this.style.visibility='hidden'" title="Play">
    <span class="e-title clamp-2 e-play" data-e-play="${e.index}" title="Play">${esc(e.title)}${e.uploader ? `<span class="faint"> · ${esc(e.uploader)}</span>` : ""}</span>
    <span class="e-dur mono">${e.duration ? fmtDuration(e.duration) : ""}</span>
    ${off ? "" : `<span class="e-act">
      <button type="button" class="btn-icon sm" data-e-cart="${e.index}" aria-label="Add to cart" title="Add to cart"><i class="bi bi-cart-plus"></i></button>
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

/** One track of the previewed playlist: play (title/thumb), cart, or download. */
async function onEntryAction(e) {
  const btn = e.target.closest("[data-e-play], [data-e-cart], [data-e-dl]");
  if (!btn) return;
  const idx = Number(btn.dataset.ePlay || btn.dataset.eCart || btn.dataset.eDl);
  const entry = S.preview?.entries.find((x) => x.index === idx);
  if (!entry) return;
  e.preventDefault();  // the row is a <label>: do not toggle its checkbox
  if (btn.dataset.ePlay != null) {
    const avail = S.preview.entries.filter((x) => x.available);
    window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: avail.map(ytTrack), start: Math.max(0, avail.indexOf(entry)) } }));
  } else if (btn.dataset.eCart != null) {
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

/** Build a submit body for ``urls`` using the saved defaults for ``kind`` (audio|video). */
function buildBody(urls, kind = "audio") {
  const isAudio = kind === "audio";
  const format = isAudio ? S.audio.format : S.video.format;
  const body = {
    urls,
    format,
    playlist_mode: S.o.plmode,
    embed_metadata: S.o.meta,
    embed_thumbnail: isAudio && S.o.thumb && !NO_COVER.has(format),
    overwrite: S.o.over,
    sponsorblock: S.o.sb,
    numbering: S.o.num,
  };
  if (isAudio && LOSSY.has(format)) body.quality = S.audio.q;
  if (!isAudio) {
    body.quality = urls.length === 1 && urls[0] === S.previewFor ? effectiveVideoQuality() : S.video.q;
    if (S.o.subs) {
      body.subtitles = true;
      body.subtitle_langs = S.o.langs.split(",").map((x) => x.trim()).filter(Boolean);
    }
  }
  if (S.o.speed) body.speed_limit = Number(S.o.speed);
  if (S.o.folder.trim()) body.folder = S.o.folder.trim();
  return body;
}

/** Options for a given kind (cart / other submitters). Folder excluded. */
export function currentOptions(kind = "audio") {
  const { urls, playlist_mode, folder, ...opts } = buildBody([], kind);  // eslint-disable-line no-unused-vars
  return opts;
}

/** Snapshot so the cart / UI can show both audio and video defaults. */
export function formatState() {
  return {
    audio: { ...S.audio }, video: { ...S.video },
    audio_formats: FORMATS.audio, video_formats: FORMATS.video,
    audio_bitrates: S.cfg?.audio_bitrates || ["64", "128", "192", "256", "320"],
    video_heights: S.cfg?.video_heights || ["144", "240", "360", "480", "720", "1080", "1440", "2160"],
  };
}

/** Apply one type's format/quality into saved defaults (used when cart edits are dropped — keep API). */
export function applyFormatState({ kind, format, q } = {}) {
  if (kind === "audio") {
    if (format && FORMATS.audio.includes(format)) S.audio.format = format;
    if (q?.audio != null) S.audio.q = String(q.audio);
    else if (typeof q === "string") S.audio.q = q;
  } else if (kind === "video") {
    if (format && FORMATS.video.includes(format)) S.video.format = format;
    if (q?.video != null) S.video.q = String(q.video);
    else if (typeof q === "string" || typeof q === "number") S.video.q = String(q);
  }
  refreshAll();
}

/** "MP3 · 192 kbps" for one kind (or a cart-style state object). */
export function formatLabel(state = null) {
  if (state?.kind === "audio" || state?.kind === "video") {
    const kind = state.kind;
    const format = state.format || (kind === "audio" ? S.audio.format : S.video.format);
    const qq = state.q || {};
    if (kind === "audio") {
      const aq = qq.audio ?? S.audio.q;
      const q = LOSSY.has(format) ? (aq === "best" ? "best" : `${aq} kbps`) : "";
      return `${format.toUpperCase()}${q ? " · " + q : ""}`;
    }
    const vq = qq.video ?? S.video.q;
    const q = vq === "best" ? "best" : HEIGHT_LABEL[vq] || `${vq}p`;
    return `${format.toUpperCase()} · ${q}`;
  }
  return `${kindLabel("audio")} · ${kindLabel("video")}`;
}

function selectionSpec() {
  const p = S.preview;
  if (!p || p.kind !== "playlist" || S.urls.length !== 1) return null;
  const avail = p.entries.filter((e) => e.available).length;
  return S.selection.size === avail ? null : compressSelection([...S.selection]);
}

async function submit(urls, btn, { kind } = {}) {
  let useKind = kind;
  if (!useKind) {
    useKind = await pickDownloadKind();
    if (!useKind) return null;
  }
  const body = buildBody(urls, useKind);
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
    const res = await submit(S.urls, $("#go-btn"));
    if (!res) return;
    $("#url-input").value = "";
    S.urls = []; S.o.folder = ""; if ($("#o-folder")) $("#o-folder").value = "";
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

function formatSummaryText() {
  return `Audio ${kindLabel("audio")} · Video ${kindLabel("video")}`;
}

/** Collapsed download rail — Settings + Download/Cart live in the panel. */
function setComposerRail(open) {
  const rail = $("#composer-rail");
  const panel = $("#composer-rail-panel");
  const toggle = $("#composer-rail-toggle");
  if (!rail || !panel || !toggle) return;
  const on = !!open;
  rail.classList.toggle("is-open", on);
  panel.hidden = !on;
  toggle.setAttribute("aria-expanded", on ? "true" : "false");
  const hint = toggle.querySelector(".composer-rail-hint");
  if (hint) hint.textContent = on ? "tap to collapse" : "tap to expand";
}

function composerRailOpen() {
  return $("#composer-rail")?.classList.contains("is-open");
}

/** Open the rail when there is something to download (Link paste / playlist pick). */
function ensureComposerRailForSubmit() {
  const n = S.urls.length;
  const sel = S.preview?.kind === "playlist" && n === 1 ? S.selection.size : null;
  if (n > 0 && sel !== 0) setComposerRail(true);
}

function updateGo() {
  const btn = $("#go-btn"), label = $("#go-label"), sub = $("#go-sub");
  const n = S.urls.length;
  const sel = S.preview?.kind === "playlist" && n === 1 ? S.selection.size : null;
  btn.disabled = n === 0 || sel === 0;
  $("#cart-add-btn").disabled = n === 0 || sel === 0;
  label.textContent = sel != null ? (sel === 0 ? "Select at least one track" : `Download ${sel} track${sel === 1 ? "" : "s"}`) : n > 1 ? `Download ${n} links` : "Download";
  const fmt = formatSummaryText();
  const dest = S.cfg?.server_mode ? " · pick Audio or Video when you download" : S.preview ? ` · downloads/${S.preview.folder_preview}` : " · pick Audio or Video when you download";
  if (sub) {
    sub.innerHTML = `${esc(fmt)}${esc(dest)} · <button type="button" class="linkish" data-open-dl-defaults>change</button>`;
  }
  const chip = $("#dl-defaults-summary");
  if (chip) chip.textContent = fmt;
  const detail = $("#dl-defaults-detail");
  if (detail) detail.textContent = fmt;
  ensureComposerRailForSubmit();
}

// ───────────────────────── search ─────────────────────────
// Results paint in the main Explore feed (``feedSearch``), not under this form.

function doSearch(e, page = 1, { fromRoute = false, replace = false } = {}) {
  e?.preventDefault?.();
  const q = $("#search-input").value.trim();
  if (q.length < 2) return;
  const music = !!getUiPrefs().omnibarMusic;
  const want = Math.max(1, Math.min(10, Number(page) || 1));
  const omni = $("#omnibar-input");
  if (omni) omni.value = q;
  window.dispatchEvent(new CustomEvent("feed-search", {
    detail: { q, page: want, fromRoute, replace, music },
  }));
}

/** Track cards + bulk bar — click a card to play; cart / download stay on the action strip. */
function resultsHTML(list) {
  return list.map((r, i) => `
      <div class="result" data-i="${i}" data-key="yt:${esc(r.video_id)}" style="--i:${i};animation-delay:${Math.min(i, 14) * 35}ms" role="button" tabindex="0" title="Play">
        <img class="r-thumb" loading="lazy" alt="" src="${esc(r.thumbnail || "")}" onerror="this.style.visibility='hidden'">
        <div class="r-meta">
          <div class="r-title clamp-2">${esc(r.title)}</div>
          <div class="r-sub">${esc(r.uploader || "")}${r.duration ? ` · ${fmtDuration(r.duration)}` : ""}</div>
        </div>
        <div class="r-acts">
          <button class="btn-icon" data-cart="${i}" aria-label="Add to cart" title="Add to cart"><i class="bi bi-cart-plus"></i></button>
          <button class="btn-icon ok" data-dl="${i}" aria-label="Download" title="Download"><i class="bi bi-download"></i></button>
        </div>
      </div>`).join("") + `<div class="results-toolbar">
        <button class="btn-ghost" data-play-all><i class="bi bi-play-circle"></i>Play all</button>
        <button class="btn-ghost" data-shuffle-all><i class="bi bi-shuffle"></i>Shuffle</button>
        <button class="btn-ghost" data-cart-all><i class="bi bi-cart-plus"></i>All to cart</button>
        <button class="btn-ghost" data-dl-all><i class="bi bi-cloud-download"></i>Download all ${list.length}</button></div>`;
}

/** Click handler for a ``.results`` list; ``list()`` returns the entries it currently shows. */
async function onResultClick(e, list = () => S.searchResults) {
  const items = list();
  if (e.target.closest("[data-shuffle-all]")) {
    window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: items.map(ytTrack), shuffle: true } }));
    return;
  }
  if (e.target.closest("[data-play-all]")) {
    window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: items.map(ytTrack), start: 0 } }));
    return;
  }
  const cartOne = e.target.closest("[data-cart]");
  if (cartOne) { toCart([items[+cartOne.dataset.cart]]); cartOne.classList.add("added"); return; }
  if (e.target.closest("[data-cart-all]")) { toCart(items); $$("[data-cart]", e.currentTarget).forEach((b) => b.classList.add("added")); return; }
  const one = e.target.closest("[data-dl]");
  const all = e.target.closest("[data-dl-all]");
  if (one || all) {
    const btn = one || all;
    try {
      const urls = one ? [items[+one.dataset.dl].url] : items.map((r) => r.url);
      await submit(urls, btn);
    } catch (err) { toast(err.detail || err.message, "err"); }
    return;
  }
  // Card body → play that track in context of the list
  const card = e.target.closest(".result[data-i]");
  if (!card || e.target.closest(".r-acts")) return;
  const start = Number(card.dataset.i);
  if (!Number.isFinite(start) || !items[start]) return;
  window.dispatchEvent(new CustomEvent("player-play", { detail: { tracks: items.map(ytTrack), start } }));
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

function setSource(src, { focus = true, fromRoute = false } = {}) {
  S.src = src;
  $$("#src-seg button").forEach((b) => b.classList.toggle("active", b.dataset.src === src));
  $("#src-link").hidden = src !== "link";
  $("#src-search").hidden = src !== "search";
  $("#src-account").hidden = src !== "account";
  if (!fromRoute) {
    const patch = { page: "home", tab: tabFromSrc(src) };
    if (src !== "search") { patch.q = ""; patch.sp = 1; }
    navigateTo(patch, { replace: false, reason: "tab" });
  }
  if (src === "account") loadAccount();
  else if (!focus) return;
  else if (src === "search") setTimeout(() => $("#search-input")?.focus(), 50);
  else $("#url-input")?.focus();
}

export function focusInput() { setSource("link"); $("#url-input").focus(); }

/** Top omnibar / paste: drop links into the Link pane and open New download. */
export function acceptLinks(text) {
  setSource("link");
  const box = $("#url-input");
  box.value = String(text || "").trim();
  readUrls();
  window.dispatchEvent(new CustomEvent("go-new"));
  setTimeout(() => box.focus(), 40);
}

/**
 * Fill the Search tab + paint results in the main home feed.
 * When ``results`` is passed, paint them without a second network round-trip.
 */
export function acceptSearchQuery(q, { music, submit = true, results = null, page = 1, pages = 1, focus = false } = {}) {
  setSource("search", { focus });
  $("#search-input").value = String(q || "");
  if (music != null && getUiPrefs().omnibarMusic !== !!music) {
    // Only write when it actually flips — avoids ui-prefs → search feedback loops.
    setUiPrefs({ omnibarMusic: !!music });
  }
  if (focus) window.dispatchEvent(new CustomEvent("go-new"));
  const query = String(q || "").trim();
  if (results) {
    S.searchResults = results;
    window.dispatchEvent(new CustomEvent("feed-search", {
      detail: {
        q: query, page, results, pages, replace: true,
        music: music != null ? !!music : undefined,
      },
    }));
    return;
  }
  if (submit && query.length >= 2) {
    window.dispatchEvent(new CustomEvent("feed-search", {
      detail: { q: query, page, music: music != null ? !!music : undefined },
    }));
  }
}

export const renderResultsHTML = (list) => resultsHTML(list);
export const handleResultClick = (e, listFn) => onResultClick(e, listFn);

export function initComposer(cfg) {
  S.cfg = cfg;
  S.o.thumb = cfg.embed_thumbnail; S.o.meta = cfg.embed_metadata; S.o.num = cfg.playlist_numbering !== false;
  const fmt = cfg.default_format;
  if (cfg.audio_formats?.includes(fmt)) S.audio.format = fmt;
  else if (cfg.video_formats?.includes(fmt)) S.video.format = fmt;
  if (cfg.default_audio_quality) S.audio.q = String(cfg.default_audio_quality);
  if (cfg.default_video_quality) S.video.q = String(cfg.default_video_quality);
  const saved = store("mst.prefs");
  if (saved) {
    // New shape: { audio, video, o }
    if (saved.audio?.format && FORMATS.audio.includes(saved.audio.format)) S.audio.format = saved.audio.format;
    if (saved.audio?.q != null) S.audio.q = String(saved.audio.q);
    if (saved.video?.format && FORMATS.video.includes(saved.video.format)) S.video.format = saved.video.format;
    if (saved.video?.q != null) S.video.q = String(saved.video.q);
    // Legacy shape: { kind, format, q }
    if (saved.kind === "audio" && FORMATS.audio.includes(saved.format)) {
      S.audio.format = saved.format;
      if (saved.q?.audio != null) S.audio.q = String(saved.q.audio);
    }
    if (saved.kind === "video" && FORMATS.video.includes(saved.format)) {
      S.video.format = saved.format;
      if (saved.q?.video != null) S.video.q = String(saved.q.video);
    }
    if (saved.q?.audio != null && !saved.audio) S.audio.q = String(saved.q.audio);
    if (saved.q?.video != null && !saved.video) S.video.q = String(saved.q.video);
    Object.assign(S.o, saved.o || {}, { folder: "" });
  }

  refreshAll();

  $("#src-seg").addEventListener("click", (e) => { const b = e.target.closest("[data-src]"); if (b) setSource(b.dataset.src); });
  $("#audio-format-chips")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-af]"); if (!b) return;
    S.audio.format = b.dataset.af; refreshAll();
  });
  $("#audio-quality-chips")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-aq]"); if (!b) return;
    S.audio.q = b.dataset.aq; refreshAll();
  });
  $("#video-format-chips")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-vf]"); if (!b) return;
    S.video.format = b.dataset.vf; refreshAll();
  });
  $("#video-quality-chips")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-vq]"); if (!b) return;
    S.video.q = b.dataset.vq; refreshAll();
  });
  $("#pl-seg")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-mode]");
    if (!b) return;
    S.o.plmode = b.dataset.mode; renderOptions(); savePrefs();
    if (S.urls.length === 1) analyze(S.urls[0]);
  });

  // switches/selects re-render dependent UI; free-text inputs only store (re-rendering would fight the caret)
  const bind = (id, key, prop = "checked", rerender = true) => {
    const el = $(id); if (!el) return;
    el.addEventListener("input", (e) => {
      S.o[key] = e.target[prop];
      if (rerender) renderOptions();
      savePrefs();
    });
  };
  bind("#o-thumb", "thumb"); bind("#o-meta", "meta"); bind("#o-subs", "subs"); bind("#o-over", "over"); bind("#o-sb", "sb"); bind("#o-num", "num");
  bind("#o-speed", "speed", "value");
  bind("#o-langs", "langs", "value", false); bind("#o-folder", "folder", "value", false);

  $("#composer-rail-toggle")?.addEventListener("click", () => setComposerRail(!composerRailOpen()));
  $("#open-dl-defaults")?.addEventListener("click", () => window.dispatchEvent(new CustomEvent("open-settings", { detail: { focus: "dl-defaults" } })));
  $("#go-sub")?.addEventListener("click", (e) => {
    if (e.target.closest("[data-open-dl-defaults]")) {
      window.dispatchEvent(new CustomEvent("open-settings", { detail: { focus: "dl-defaults" } }));
    }
  });

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
  $("#acc-feed").addEventListener("click", (e) => onResultClick(e, () => S.feed));
  $("#acc-playlists").addEventListener("click", (e) => { const c = e.target.closest("[data-pl]"); if (c) openAccountPlaylist(+c.dataset.pl); });
  $("#acc-msg").addEventListener("click", (e) => { if (e.target.closest("[data-acc-cookies]")) window.dispatchEvent(new CustomEvent("open-cookies")); });
  $("#acc-refresh").addEventListener("click", () => loadAccount(true));
  window.addEventListener("cookies-changed", () => { S.accLoaded = false; if (S.src === "account") loadAccount(true); });
  window.addEventListener("player-change", (e) => {
    document.querySelectorAll("#acc-feed .result, #explore-results .result").forEach((el) => el.classList.toggle("is-playing", !!e.detail.key && el.dataset.key === e.detail.key));
  });
  // "Download this" button on the mini player (YouTube tracks)
  window.addEventListener("queue-download", async (e) => {
    try { await submit(e.detail.urls, null); } catch (err) { toast(err.detail || err.message, "err"); }
  });
  refreshFolders();
  window.addEventListener("folders-changed", refreshFolders);

  // Address bar → restore composer tab / fill search box (feed restore lives in explore.js)
  window.addEventListener("route", (e) => {
    const r = e.detail || {};
    if (r.page && r.page !== "home") return;
    if (r.reason !== "popstate" && r.reason !== "init") return;
    const src = srcFromTab(r.tab || "link");
    if (S.src !== src) setSource(src, { focus: false, fromRoute: true });
    if (src === "search" && r.q) {
      const box = $("#search-input");
      if (box) box.value = r.q;
      const omni = $("#omnibar-input");
      if (omni) omni.value = r.q;
    }
  });

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
