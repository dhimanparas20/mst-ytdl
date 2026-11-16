// The in-app player: a docked control bar + a floating video window.
//
// Sources
//   • library files      → plain <video>/<audio> on /api/library/file (Range, seekable)
//   • YouTube videos     → /api/stream/<id>/media, i.e. OUR server relays the CDN stream. No YouTube
//                          iframe, so nothing is "embed-blocked". Muxed formats play from one element;
//                          anything above the muxed ceiling plays as a muted <video> + a separate
//                          <audio> that is the clock master, with drift correction ("dual" mode).
//
// Pure logic (shuffle, reorder, quality choice, sleep maths) lives in queue-logic.js and is unit-tested.

import { api } from "./api.js";
import {
  RATES, SLEEP_OPTIONS, fadeVolume, fmtClock, makeBag, moveItem, nextIndex, pickQuality, pickSubtitle,
  qualityLabel, removeItem, shuffleRest, sleepRemaining,
} from "./queue-logic.js";
import { toast } from "./ui.js";
import { initWindow, hideWindow, isVisible as windowVisible, showWindow, toggleFullscreen } from "./vwin.js";
import { $, $$, esc, fmtDuration, store } from "./util.js";

export { RATES };
const PREFS_KEY = "mst.player";
export const AUDIO_EXT = new Set(["mp3", "m4a", "opus", "flac", "wav", "ogg", "aac"]);
export const VIDEO_EXT = new Set(["mp4", "mkv", "webm", "mov"]);
const extOf = (name) => (String(name).split(".").pop() || "").toLowerCase();
export const isAudioName = (n) => AUDIO_EXT.has(extOf(n));
export const isVideoName = (n) => VIDEO_EXT.has(extOf(n));
export const isMediaName = (n) => isAudioName(n) || isVideoName(n);

// ───────────────────────── track helpers ─────────────────────────

/** From a /api/search, playlist-preview or EntryInfo object. */
export const ytTrack = (e) => ({
  kind: "yt", key: `yt:${e.video_id}`, id: e.video_id, title: e.title || "YouTube video", artist: e.uploader || "YouTube",
  thumb: e.thumbnail, duration: e.duration, url: e.url || `https://www.youtube.com/watch?v=${e.video_id}`,
});

/** From a /api/library entry (or {path, title}); ``url`` overrides where it streams from (server mode: /api/items/<id>/file). */
export const fileTrack = (e) => ({
  kind: "file", key: `file:${e.path}`, path: e.path, url: e.url || null, video: isVideoName(e.path),
  title: (e.title || e.name || e.path.split("/").pop()).replace(/\.[a-z0-9]{2,4}$/i, ""),
  artist: e.artist || (e.path.includes("/") ? e.path.split("/").slice(-2, -1)[0] : "Library"),
  // audio files: the cover embedded in the tags (404 → the UI falls back to the note icon)
  thumb: e.thumb || (isVideoName(e.path) ? null : api.libCoverUrl(e.path)), duration: e.duration || null,
});

// ───────────────────────── state ─────────────────────────

const S = {
  queue: [], index: -1, bag: [], history: [],
  playing: false, loading: false, repeat: "off", shuffle: false,
  volume: 0.8, muted: false, rate: 1,
  quality: "auto", picked: null, info: null,
  subs: [], subSel: null, subOn: false, subLang: "en", subSize: "m",
  windowWanted: true, forcePoster: false,
  seeking: false, queueOpen: false, sleep: null, failStreak: 0, recoveries: 0,
};
const E = { master: null, dual: false, token: 0, vStall: false, switching: false };
let V, A;        // <video id=mp-v>, <audio id=mp-a>
let tickTimer = null;

const current = () => S.queue[S.index] || null;
const hasPicture = () => !!E.master && (E.master === V || E.dual);
const dur = () => (E.master && isFinite(E.master.duration) ? E.master.duration : 0) || current()?.duration || 0;
const now = () => (E.master ? E.master.currentTime || 0 : 0);

// ───────────────────────── persistence ─────────────────────────

function loadPrefs() {
  const p = store(PREFS_KEY) || {};
  if (typeof p.volume === "number") S.volume = Math.max(0, Math.min(1, p.volume));
  if (typeof p.muted === "boolean") S.muted = p.muted;
  if (RATES.includes(p.rate)) S.rate = p.rate;
  if (["off", "all", "one"].includes(p.repeat)) S.repeat = p.repeat;
  if (typeof p.shuffle === "boolean") S.shuffle = p.shuffle;
  if (p.quality != null) S.quality = p.quality;
  if (typeof p.subOn === "boolean") S.subOn = p.subOn;
  if (typeof p.subLang === "string") S.subLang = p.subLang;
  if (["s", "m", "l"].includes(p.subSize)) S.subSize = p.subSize;
  if (typeof p.windowWanted === "boolean") S.windowWanted = p.windowWanted;
}
const savePrefs = () => store(PREFS_KEY, {
  volume: S.volume, muted: S.muted, rate: S.rate, repeat: S.repeat, shuffle: S.shuffle, quality: S.quality,
  subOn: S.subOn, subLang: S.subLang, subSize: S.subSize, windowWanted: S.windowWanted,
});

// ───────────────────────── engine ─────────────────────────

function clearMedia() {
  E.switching = true;
  for (const m of [V, A]) { m.pause(); m.removeAttribute("src"); m.load(); }
  V.querySelectorAll("track").forEach((t) => t.remove());
  E.master = null; E.dual = false; E.vStall = false;
  setTimeout(() => { E.switching = false; }, 0);
}

function applyMediaSettings() {
  for (const m of [V, A]) { m.defaultPlaybackRate = S.rate; m.playbackRate = S.rate; }
  const fade = fadeVolume(S.volume, sleepRemaining(S.sleep));
  A.volume = fade; V.volume = fade;
  A.muted = S.muted;
  V.muted = E.dual ? true : S.muted;   // in dual mode the picture is silent; the <audio> carries the sound
}

function seekTo(sec) {
  if (!E.master) return;
  const t = Math.max(0, Math.min(sec, dur() || sec));
  E.master.currentTime = t;
  if (E.dual) V.currentTime = t;
}

function play() {
  // a rejected play() (autoplay policy, aborted load) must not leave the UI claiming "playing"
  E.master?.play().catch((e) => { if (e?.name !== "AbortError" && !E.vStall) { setPlaying(false); setLoading(false); } });
}
function pause() { E.master?.pause(); }
export function toggle() {
  if (!E.master || !current()) return;
  const wasPlaying = S.playing;
  wasPlaying ? pause() : play();
  pulse(wasPlaying ? "pause-fill" : "play-fill");
}

function setPlaying(v) {
  if (S.playing === v) return;
  S.playing = v;
  document.body.classList.toggle("is-playing-now", v);
  renderControls();
  emitChange();
}

function setLoading(v) {
  S.loading = v;
  $("#mp-play")?.classList.toggle("loading", v);
  $("#vwin-spinner").hidden = !v;
}

function showMessage(html) {
  const m = $("#vwin-msg");
  m.hidden = !html;
  m.innerHTML = html || "";
}

function pulse(icon) {
  const p = $("#vwin-pulse");
  p.querySelector("i").className = `bi bi-${icon}`;
  p.classList.remove("go"); void p.offsetWidth; p.classList.add("go");
}

/** Stream a library file or a YouTube video. Async because YouTube stream info takes a few seconds. */
async function loadTrack(t, { resumeAt = 0, autoplay = true } = {}) {
  const my = ++E.token;
  setLoading(true);
  showMessage("");
  clearMedia();
  S.info = null; S.picked = null; S.subs = []; S.subSel = null; S.recoveries = 0;
  renderMenus();
  $("#vposter-img").src = t.thumb || "";

  if (t.kind === "file") {
    const url = t.url || api.fileUrl(t.path);
    if (t.video) { E.master = V; V.src = url; } else { E.master = A; A.src = url; }
    finishAttach(my, resumeAt, autoplay);
    loadFileSubs(t, my);
    return;
  }

  let info;
  try { info = await api.streamInfo(t.id); }
  catch (e) { if (my === E.token) fail(e.detail || e.message, t); return; }
  if (my !== E.token) return;           // user already moved on
  if (info.live) { fail("Live streams can't be played here.", t); return; }
  S.info = info;
  if (!t.thumb && info.thumbnail) { t.thumb = info.thumbnail; renderTrack(); }
  S.subs = info.subtitles.map((s) => ({ kind: "yt", lang: s.lang, auto: s.auto, label: s.name }));
  attachYt(info, resumeAt, autoplay, my);
  const pick = pickSubtitle(S.subs, S.subLang);
  if (S.subOn && pick) setSubtitle(pick, { remember: false });
  renderMenus();
}

function attachYt(info, resumeAt, autoplay, my = E.token) {
  const t = current();
  const pick = pickQuality(info.qualities, S.quality);
  S.picked = pick;
  E.switching = true;
  V.removeAttribute("src"); A.removeAttribute("src");
  V.querySelectorAll("track").forEach((x) => x.remove());
  if (!pick || pick.audio) {
    if (!info.audio_format) { fail("No playable stream was found for this video.", t); return; }
    E.master = A; E.dual = false;
    A.src = api.streamUrl(info.video_id, info.audio_format);
  } else {
    const o = pick.option;
    E.dual = !o.muxed;
    E.master = E.dual ? A : V;
    V.src = api.streamUrl(info.video_id, o.video_format);
    if (E.dual) A.src = api.streamUrl(info.video_id, o.audio_format);
  }
  setTimeout(() => { E.switching = false; }, 0);
  finishAttach(my, resumeAt, autoplay);
  if (S.subSel && S.subSel.kind === "yt") setSubtitle(S.subSel, { remember: false });
}

function finishAttach(my, resumeAt, autoplay) {
  applyMediaSettings();
  syncWindow();
  const master = E.master;
  const onMeta = () => {
    if (my !== E.token) return;
    if (resumeAt) seekTo(resumeAt);
    renderTime();
  };
  master.addEventListener("loadedmetadata", onMeta, { once: true });
  if (E.dual) V.addEventListener("loadedmetadata", () => { if (my === E.token) V.currentTime = A.currentTime || resumeAt || 0; }, { once: true });
  renderMenus();
  if (autoplay) play();
  updateMediaSession();
  startTick();
}

/** Re-resolve a stream whose signed URL went stale and carry on from the same second. */
async function recover() {
  const t = current();
  if (!t || t.kind !== "yt" || S.recoveries >= 2) return false;
  S.recoveries++;
  const at = now();
  const wasPlaying = S.playing || S.loading;
  setLoading(true);
  try {
    const info = await api.streamInfo(t.id);
    S.info = info;
    attachYt(info, at, wasPlaying);
    return true;
  } catch { return false; }
}

function fail(message, track) {
  setLoading(false);
  const yt = track?.kind === "yt";
  showMessage(`<div><i class="bi bi-exclamation-octagon-fill" style="font-size:26px"></i></div><div>${esc(message)}</div>
    ${yt ? `<a href="${esc(track.url)}" target="_blank" rel="noopener">Open on YouTube <i class="bi bi-box-arrow-up-right"></i></a>` : ""}`);
  toast(`${track?.title ? track.title + " — " : ""}${message}`, "warn", { title: "Playback" });
  S.failStreak++;
  if (S.queue.length > 1 && S.failStreak < S.queue.length) setTimeout(() => next(), 900);
}

// ───────────────────────── subtitles ─────────────────────────

async function loadFileSubs(t, my) {
  if (!t.video || t.url) return;  // queue files streamed by item id are not library paths
  try {
    const list = await api.libSubtitles(t.path);
    if (my !== E.token) return;
    S.subs = list.map((s) => ({ kind: "file", id: s.id, lang: s.lang, auto: false, label: s.label }));
    const pick = pickSubtitle(S.subs, S.subLang);
    if (S.subOn && pick) setSubtitle(pick, { remember: false });
    renderMenus();
  } catch { /* no subtitles for this file */ }
}

function setSubtitle(sel, { remember = true } = {}) {
  V.querySelectorAll("track").forEach((x) => x.remove());
  S.subSel = sel;
  if (remember) { S.subOn = !!sel; if (sel) S.subLang = sel.lang; savePrefs(); }
  if (sel) {
    const t = current();
    const tr = document.createElement("track");
    tr.kind = "subtitles"; tr.label = sel.label; tr.srclang = String(sel.lang || "und").slice(0, 8);
    tr.src = sel.kind === "yt" ? api.streamSubsUrl(t.id, sel.lang, sel.auto) : api.libSubtitleUrl(t.path, sel.id);
    tr.default = true;
    tr.addEventListener("error", () => {
      // Auto-load often 429s without cookies; only toast when the user picked a track.
      if (remember) toast("Those subtitles could not be loaded.", "warn");
      S.subSel = null;
      renderMenus();
    });
    V.appendChild(tr);
    requestAnimationFrame(() => { for (const x of V.textTracks) x.mode = x.label === sel.label ? "showing" : "disabled"; });
  }
  renderMenus();
}

function cycleSubtitle() {
  if (!S.subs.length) return toast("This video has no subtitles.", "info", { ms: 1800 });
  const i = S.subSel ? S.subs.findIndex((s) => s.kind === S.subSel.kind && s.lang === S.subSel.lang && s.auto === S.subSel.auto && (s.id || "") === (S.subSel.id || "")) : -1;
  setSubtitle(i + 1 < S.subs.length ? S.subs[i + 1] : null);
}

// ───────────────────────── quality ─────────────────────────

function setQuality(pref) {
  S.quality = pref;
  savePrefs();
  const t = current();
  if (t?.kind === "yt" && S.info) attachYt(S.info, now(), S.playing || S.loading);
  else renderMenus();
}

// ───────────────────────── queue ─────────────────────────

function emitChange() {
  const t = current();
  window.dispatchEvent(new CustomEvent("player-change", { detail: { key: t?.key || null, playing: S.playing } }));
}

/** Next index for a skip/auto-advance (``manual`` skips treat "repeat one" like "repeat all"). */
function advance(manual) {
  const repeat = S.repeat === "one" ? (manual ? "all" : "off") : S.repeat;
  const r = nextIndex({ n: S.queue.length, index: S.index, shuffle: S.shuffle, repeat, bag: S.bag });
  S.bag = r.bag;
  return r.index;
}

function onEnded() {
  if (S.sleep?.mode === "track") { clearSleep("Sleep timer: stopped after this track"); setPlaying(false); return; }
  if (S.repeat === "one") { seekTo(0); play(); return; }
  S.failStreak = 0;
  const i = advance(false);
  if (i === null) { setPlaying(false); seekTo(0); renderTime(); return; }
  playIndex(i);
}

export function next() {
  const i = advance(true);
  if (i === null) { pause(); return; }
  playIndex(i);
}

export function prev() {
  if (!S.queue.length) return;
  if (E.master && now() > 3) { seekTo(0); return; }
  const back = S.history.pop();
  if (back != null && S.queue[back]) return playIndex(back, { back: true });
  playIndex(S.index > 0 ? S.index - 1 : S.repeat === "all" ? S.queue.length - 1 : 0, { back: true });
}

export async function playIndex(i, { back = false } = {}) {
  const t = S.queue[i];
  if (!t) return;
  if (!back && S.index >= 0 && S.index !== i) S.history.push(S.index);
  if (S.history.length > 200) S.history.shift();
  S.index = i;
  S.bag = S.bag.filter((x) => x !== i);
  showDock();
  renderTrack(true);
  renderQueue();
  emitChange();
  await loadTrack(t);
}

/** Replace the queue and start. ``shuffle: true`` turns shuffle on and starts at a random track. */
export function playTracks(tracks, start = 0, { shuffle = false } = {}) {
  if (!tracks.length) return;
  S.queue = tracks.slice();
  S.history = [];
  S.failStreak = 0;
  if (shuffle) { S.shuffle = true; start = Math.floor(Math.random() * tracks.length); savePrefs(); }
  const s = Math.max(0, Math.min(start, tracks.length - 1));
  S.bag = makeBag(tracks.length, s);
  renderControls(); renderQueue();
  playIndex(s);
}

export function enqueue(tracks) {
  if (!tracks.length) return;
  const known = new Set(S.queue.map((t) => t.key));
  const fresh = tracks.filter((t) => !known.has(t.key));
  if (!fresh.length) return toast("Already in the queue.", "info", { ms: 1600 });
  const idle = S.index < 0;
  const base = S.queue.length;
  S.queue.push(...fresh);
  S.bag.push(...fresh.map((_, k) => base + k));
  renderQueue();
  if (idle) playIndex(0); else toast(`${fresh.length} added to the queue`, "info", { ms: 1800 });
}

export function closePlayer() {
  E.token++;
  clearMedia();
  S.queue = []; S.index = -1; S.bag = []; S.history = []; S.playing = false; S.info = null;
  clearInterval(tickTimer); tickTimer = null;
  clearSleep();
  hideWindow();
  $("#miniplayer").hidden = true;
  document.body.classList.remove("has-player", "is-playing-now", "has-window");
  emitChange();
}

// ───────────────────────── sleep timer ─────────────────────────

function setSleep(opt) {
  if (opt === "off") return clearSleep("Sleep timer off");
  S.sleep = opt === "track" ? { mode: "track" } : { mode: "time", at: Date.now() + Number(opt) * 60_000 };
  toast(opt === "track" ? "Will stop after this track" : `Will pause in ${opt} minutes`, "info", { ms: 2200 });
  renderSleep(); renderMenus();
}

function clearSleep(message) {
  const had = !!S.sleep;
  S.sleep = null;
  applyMediaSettings();
  renderSleep(); renderMenus();
  if (message && had) toast(message, "info", { ms: 2400 });
}

function tickSleep() {
  const rem = sleepRemaining(S.sleep);
  if (rem === null) return;
  renderSleep();
  applyMediaSettings();      // fades the volume during the last seconds
  if (rem <= 0) { pause(); clearSleep("Sleep timer: paused"); }
}

// ───────────────────────── rendering ─────────────────────────

function showDock() {
  $("#miniplayer").hidden = false;
  document.body.classList.add("has-player");
}

/** Show the video window only when there is a picture (or the user asked for the poster). */
function syncWindow() {
  const wantsIt = S.windowWanted && (hasPicture() || S.forcePoster);
  wantsIt ? showWindow() : hideWindow();
  $("#vwin-poster").hidden = hasPicture();
  $("#mp-window").classList.toggle("on", windowVisible());
  $("#mp-window").hidden = false;
  $("#vw-pip").hidden = !document.pictureInPictureEnabled || !hasPicture();
}

function renderTrack(animate = false) {
  const t = current();
  if (!t) return;
  const title = $("#mp-title"), sub = $(".mp-sub");
  title.textContent = t.title; title.title = t.title;
  $("#mp-artist").textContent = t.artist || "";
  $("#vwin-title").textContent = t.title;
  if (animate) for (const el of [title, sub]) { el.classList.remove("swap"); void el.offsetWidth; el.classList.add("swap"); }
  const img = $("#mp-thumb");
  img.onerror = () => { if (t.thumb) { t.thumb = null; renderTrack(); } };   // no cover: icon instead
  if (t.thumb) { img.src = t.thumb; img.hidden = false; } else img.hidden = true;
  $("#mp-art-fallback").hidden = !!t.thumb;
  const amb = $("#mp-ambient");
  amb.style.backgroundImage = t.thumb ? `url("${t.thumb}")` : "none";
  amb.classList.toggle("on", !!t.thumb);
  $("#mp-download").hidden = t.kind !== "yt";
  const stDl = $("#st-download");
  if (stDl) stDl.hidden = t.kind !== "yt";
  const tag = $("#mp-source");
  tag.hidden = false;
  tag.textContent = t.kind === "yt" ? "YouTube" : t.video ? "video" : "local";
  $("#mp-dur").textContent = t.duration ? fmtDuration(t.duration) : "0:00";
  $("#vw-dur").textContent = $("#mp-dur").textContent;
  renderTime(0);
  document.title = `${t.title} — mst-ytdl`;
  window.dispatchEvent(new CustomEvent("player-track", { detail: { key: t.key } }));
}

function renderControls() {
  for (const [sel, base] of [["#mp-play i", "bi bi-"], ["#vw-play i", "bi bi-"]]) $(sel).className = `${base}${S.playing ? "pause-fill" : "play-fill"}`;
  $("#mp-play").setAttribute("aria-label", S.playing ? "Pause" : "Play");
  $("#mp-shuffle").classList.toggle("on", S.shuffle);
  const rep = $("#mp-repeat");
  rep.classList.toggle("on", S.repeat !== "off");
  rep.querySelector("i").className = `bi bi-${S.repeat === "one" ? "repeat-1" : "repeat"}`;
  rep.title = `Repeat: ${S.repeat} (R)`;
  const v = S.muted ? 0 : S.volume;
  $("#mp-vol").value = v;
  $("#mp-vol").style.setProperty("--p", `${v * 100}%`);
  $("#mp-mute i").className = `bi bi-volume-${S.muted || v === 0 ? "mute" : v < 0.4 ? "down" : "up"}-fill`;
  $("#mp-rate").textContent = `${S.rate}×`;
  $("#vw-rate-text").textContent = `${S.rate}×`;
  const stRateBtn = $("#st-rate");
  if (stRateBtn) stRateBtn.textContent = `${S.rate}×`;
  $("#mp-queue-btn").classList.toggle("on", S.queueOpen);
  $("#st-queue-btn")?.classList.toggle("on", S.queueOpen);
  $("#mp-window").classList.toggle("on", windowVisible());
}

function bufferedEnd() {
  const m = E.master;
  if (!m || !m.buffered?.length) return 0;
  for (let i = 0; i < m.buffered.length; i++) {
    if (m.currentTime >= m.buffered.start(i) - 0.2 && m.currentTime <= m.buffered.end(i) + 0.2) return m.buffered.end(i);
  }
  return m.buffered.end(m.buffered.length - 1);
}

function renderTime(sec = now()) {
  const d = dur();
  const frac = d ? Math.min(1, sec / d) : 0;
  const buf = d ? Math.min(100, (bufferedEnd() / d) * 100) : 0;
  if (!S.seeking) {
    for (const id of ["mp", "vw"]) {
      const el = $(`#${id}-seek`);
      el.value = Math.round(frac * 1000);
      el.style.setProperty("--p", `${frac * 100}%`);
    }
  }
  for (const id of ["mp-buf", "vw-buf"]) $(`#${id}`).style.width = `${buf}%`;
  const cur = fmtDuration(sec);
  $("#mp-cur").textContent = cur; $("#vw-cur").textContent = cur;
  if (d) { $("#mp-dur").textContent = fmtDuration(d); $("#vw-dur").textContent = fmtDuration(d); }
}

function renderSleep() {
  const rem = sleepRemaining(S.sleep);
  const label = S.sleep?.mode === "track" ? "end of track" : (rem != null ? fmtClock(rem) : "");
  for (const id of ["mp", "st"]) {
    const pill = $(`#${id}-sleep-pill`);
    if (pill) pill.hidden = !S.sleep;
    const left = $(`#${id}-sleep-left`);
    if (left && S.sleep) left.textContent = label;
    $(`#${id}-sleep`)?.classList.toggle("on", !!S.sleep);
  }
}

const item = (attrs, label, { active = false, extra = "" } = {}) =>
  `<li><button class="dropdown-item ${active ? "active" : ""}" ${attrs}>${label}${extra}</button></li>`;

function renderMenus() {
  const t = current();
  // speed (dock + window + stage)
  const rates = RATES.map((r) => item(`data-rate="${r}"`, r === 1 ? "1× normal" : `${r}×`, { active: r === S.rate })).join("");
  $("#mp-rate-menu").innerHTML = rates;
  $("#vw-rate-menu").innerHTML = rates;
  const stRate = $("#st-rate-menu");
  if (stRate) stRate.innerHTML = rates;

  // quality (YouTube only)
  const qBtn = $("#vw-quality");
  const isYt = t?.kind === "yt";
  qBtn.disabled = !isYt || !S.info;
  if (isYt && S.info) {
    const q = S.info.qualities;
    $("#vw-quality-menu").innerHTML =
      `<li><h6 class="dropdown-header">Quality</h6></li>` +
      item(`data-q="auto"`, "Auto", { active: S.quality === "auto", extra: `<small>≤ 720p</small>` }) +
      q.map((o) => item(`data-q="${o.height}"`, o.label, { active: String(S.quality) === String(o.height), extra: o.muxed ? "" : `<small>HD</small>` })).join("") +
      (S.info.audio_format ? item(`data-q="audio"`, "Audio only", { active: S.quality === "audio", extra: `<small>saves data</small>` }) : "");
    $("#vw-quality-text").textContent = qualityLabel(S.quality, S.picked);
  } else {
    $("#vw-quality-menu").innerHTML = "";
    $("#vw-quality-text").textContent = t?.kind === "file" ? "Original" : "Auto";
  }

  // subtitles
  const cc = $("#vw-cc");
  const canCC = !!t && hasPicture();
  cc.disabled = !canCC;
  const sel = S.subSel;
  const sameSel = (s) => sel && s.kind === sel.kind && s.lang === sel.lang && s.auto === sel.auto && (s.id || "") === (sel.id || "");
  $("#vw-cc-menu").innerHTML =
    `<li><h6 class="dropdown-header">Subtitles</h6></li>` +
    item(`data-sub="off"`, "Off", { active: !sel }) +
    (S.subs.length
      ? S.subs.map((s, i) => item(`data-sub="${i}"`, esc(s.label || s.lang), { active: sameSel(s), extra: s.auto ? "<small>auto</small>" : "" })).join("")
      : `<li><span class="dropdown-item disabled">No subtitles for this video</span></li>`) +
    `<li><hr class="dropdown-divider"></li><li><h6 class="dropdown-header">Size</h6></li>` +
    ["s", "m", "l"].map((z) => item(`data-subsize="${z}"`, { s: "Small", m: "Medium", l: "Large" }[z], { active: S.subSize === z })).join("");
  cc.classList.toggle("on", !!sel);
  $("#vw-cc-text").textContent = sel ? String(sel.lang).slice(0, 6) : "Off";
  $("#vwin").classList.remove("sub-s", "sub-m", "sub-l");
  $("#vwin").classList.add(`sub-${S.subSize}`);

  // sleep timer (dock + stage)
  const sleepHtml =
    `<li><h6 class="dropdown-header">Sleep timer</h6></li>` +
    SLEEP_OPTIONS.map((m) => item(`data-sleep="${m}"`, `${m} minutes`)).join("") +
    item(`data-sleep="track"`, "End of this track", { active: S.sleep?.mode === "track" }) +
    (S.sleep ? `<li><hr class="dropdown-divider"></li>` + item(`data-sleep="off"`, "Turn off") : "");
  $("#mp-sleep-menu").innerHTML = sleepHtml;
  const stSleep = $("#st-sleep-menu");
  if (stSleep) stSleep.innerHTML = sleepHtml;
}

function renderQueue() {
  const n = String(S.queue.length);
  $("#mp-queue-count").textContent = n;
  const stc = $("#st-queue-count");
  if (stc) stc.textContent = n;
  const ul = $("#mp-queue-list");
  if (!ul) return;
  ul.innerHTML = S.queue.map((t, i) => `
    <li class="${i === S.index ? "now" : ""}" data-i="${i}" draggable="true" style="animation-delay:${Math.min(i, 12) * 18}ms">
      <span class="q-h" title="Drag to reorder"><i class="bi bi-grip-vertical"></i></span>
      ${t.thumb ? `<img class="q-i" src="${esc(t.thumb)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'q-i',textContent:'♪'}))">` : `<span class="q-i">${i === S.index ? '<i class="bi bi-soundwave"></i>' : i + 1}</span>`}
      <span class="truncate-1">${i === S.index ? '<i class="bi bi-soundwave me-1"></i>' : ""}${esc(t.title)}</span>
      <span class="q-k">${t.kind === "yt" ? "YouTube" : t.video ? "video" : "file"}</span>
      <button class="btn-icon sm" data-rm="${i}" aria-label="Remove from queue"><i class="bi bi-x-lg"></i></button>
    </li>`).join("");
}

function updateMediaSession() {
  if (!("mediaSession" in navigator)) return;
  const t = current();
  if (!t) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: t.title, artist: t.artist || "", album: "mst-ytdl", artwork: t.thumb ? [{ src: t.thumb, sizes: "480x360" }] : [],
  });
  navigator.mediaSession.setActionHandler("play", play);
  navigator.mediaSession.setActionHandler("pause", pause);
  navigator.mediaSession.setActionHandler("previoustrack", prev);
  navigator.mediaSession.setActionHandler("nexttrack", next);
  navigator.mediaSession.setActionHandler("seekto", (d) => { if (d.seekTime != null) seekTo(d.seekTime); });
  navigator.mediaSession.setActionHandler("seekbackward", () => seekTo(now() - 10));
  navigator.mediaSession.setActionHandler("seekforward", () => seekTo(now() + 10));
}

function startTick() {
  clearInterval(tickTimer);
  tickTimer = setInterval(() => {
    if (!E.master) return;
    if (!S.seeking) renderTime();
    // keep the silent picture locked to the audio clock
    if (E.dual && !A.paused && !V.seeking && Math.abs(V.currentTime - A.currentTime) > 0.35) V.currentTime = A.currentTime;
    // watchdog for the two-element (HD) mode: the event ping-pong between picture and sound can deadlock
    // (picture stalls → sound paused → picture paused by the sound's "waiting" → nobody resumes).
    if (E.dual && !E.switching) {
      if (E.vStall && V.readyState >= 3) { E.vStall = false; V.play().catch(() => {}); A.play().catch(() => {}); }
      else if (E.vStall && V.paused) V.play().catch(() => {});
      else if (!E.vStall && !A.paused && V.paused && V.readyState >= 2 && !V.seeking && !V.ended) V.play().catch(() => {});
    }
    tickSleep();
  }, 250);
}

// ───────────────────────── wiring ─────────────────────────

function setVolume(v) {
  S.volume = Math.max(0, Math.min(1, v));
  if (S.volume > 0) S.muted = false;
  applyMediaSettings(); renderControls(); savePrefs();
}
function setRate(r) { S.rate = r; applyMediaSettings(); renderControls(); savePrefs(); }
function toggleMute() { S.muted = !S.muted; applyMediaSettings(); renderControls(); savePrefs(); }

function bindMedia() {
  const fromMaster = (el) => el === E.master;
  for (const m of [V, A]) {
    m.addEventListener("playing", () => { if (fromMaster(m)) { setPlaying(true); setLoading(false); S.failStreak = 0; } });
    m.addEventListener("play", () => { if (fromMaster(m)) setPlaying(true); });
    m.addEventListener("pause", () => { if (fromMaster(m) && !E.switching && !E.vStall && !m.ended) setPlaying(false); });
    m.addEventListener("waiting", () => { if (fromMaster(m)) setLoading(true); });
    m.addEventListener("canplay", () => { if (fromMaster(m)) setLoading(false); });
    m.addEventListener("ended", () => { if (fromMaster(m) && !E.switching) onEnded(); });
    m.addEventListener("progress", () => { if (fromMaster(m)) renderTime(); });
    m.addEventListener("durationchange", () => { if (fromMaster(m)) renderTime(); });
    m.addEventListener("error", async () => {
      if (E.switching || !m.getAttribute("src")) return;
      const t = current();
      if (await recover()) return;
      fail(m.error?.code === 4 ? "This stream can't be played by your browser." : "The stream failed.", t);
    });
  }
  // dual mode: the audio element is the clock; the picture follows it
  A.addEventListener("play", () => { if (E.dual) V.play().catch(() => {}); });
  A.addEventListener("pause", () => { if (E.dual && !E.vStall && !E.switching) V.pause(); });
  A.addEventListener("seeking", () => { if (E.dual) V.currentTime = A.currentTime; });
  A.addEventListener("waiting", () => { if (E.dual) V.pause(); });
  // Only re-seek the picture when it really drifted: seeking it on every "playing" makes it emit "waiting",
  // which pauses the sound, which resumes... an endless stall/resume loop with the clock stuck at 0:00.
  A.addEventListener("playing", () => {
    if (!E.dual) return;
    if (Math.abs(V.currentTime - A.currentTime) > 0.3) V.currentTime = A.currentTime;
    V.play().catch(() => {});
  });
  // a "waiting" caused by our own seek is not a buffering stall
  V.addEventListener("waiting", () => { if (E.dual && !A.paused && !V.seeking) { E.vStall = true; A.pause(); setLoading(true); } });
  V.addEventListener("playing", () => { if (E.dual && E.vStall) { E.vStall = false; A.play().catch(() => {}); } });
}

function bindSeek(prefix) {
  const input = $(`#${prefix}-seek`), tip = $(`#${prefix}-tip`), wrap = input.closest(".seekwrap");
  const frac = (clientX) => { const r = input.getBoundingClientRect(); return Math.max(0, Math.min(1, (clientX - r.left) / r.width)); };
  input.addEventListener("input", () => {
    S.seeking = true; wrap.classList.add("drag");
    const f = Number(input.value) / 1000;
    for (const id of ["mp", "vw"]) { const el = $(`#${id}-seek`); el.value = input.value; el.style.setProperty("--p", `${f * 100}%`); }
    const t = fmtDuration(f * dur());
    $("#mp-cur").textContent = t; $("#vw-cur").textContent = t;
    tip.textContent = t; tip.style.left = `${f * 100}%`;
  });
  input.addEventListener("change", () => { seekTo((Number(input.value) / 1000) * dur()); S.seeking = false; wrap.classList.remove("drag"); });
  wrap.addEventListener("pointermove", (e) => { const f = frac(e.clientX); tip.textContent = fmtDuration(f * dur()); tip.style.left = `${f * 100}%`; });
}

function bindMenus() {
  const on = (sel, fn) => $(sel).addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) fn(b); });
  on("#mp-rate-menu", (b) => setRate(Number(b.dataset.rate)));
  on("#vw-rate-menu", (b) => setRate(Number(b.dataset.rate)));
  on("#st-rate-menu", (b) => setRate(Number(b.dataset.rate)));
  on("#vw-quality-menu", (b) => { if (b.dataset.q) setQuality(b.dataset.q === "auto" || b.dataset.q === "audio" ? b.dataset.q : Number(b.dataset.q)); });
  on("#vw-cc-menu", (b) => {
    if (b.dataset.sub != null) setSubtitle(b.dataset.sub === "off" ? null : S.subs[Number(b.dataset.sub)]);
    if (b.dataset.subsize) { S.subSize = b.dataset.subsize; savePrefs(); renderMenus(); }
  });
  on("#mp-sleep-menu", (b) => { if (b.dataset.sleep) setSleep(b.dataset.sleep); });
  on("#st-sleep-menu", (b) => { if (b.dataset.sleep) setSleep(b.dataset.sleep); });
  // dropdowns must escape the window's overflow:hidden → fixed positioning
  $$("#miniplayer [data-bs-toggle=dropdown], #vwin [data-bs-toggle=dropdown], #stage [data-bs-toggle=dropdown]").forEach((b) =>
    new bootstrap.Dropdown(b, { popperConfig: (d) => ({ ...d, strategy: "fixed" }) }));
}

function bindQueue() {
  const list = $("#mp-queue-list");
  list.addEventListener("click", (e) => {
    const rm = e.target.closest("[data-rm]");
    if (rm) {
      const r = removeItem(S.queue, Number(rm.dataset.rm), S.index);
      S.queue = r.list; S.index = r.index;
      if (!S.queue.length) return closePlayer();
      if (r.removedCurrent) { S.index = Math.max(-1, r.index - 1); S.bag = makeBag(S.queue.length, -1); return next(); }
      S.bag = S.bag.filter((i) => i < S.queue.length);
      return renderQueue();
    }
    const li = e.target.closest("li[data-i]");
    if (li) playIndex(Number(li.dataset.i));
  });
  let from = null;
  list.addEventListener("dragstart", (e) => { const li = e.target.closest("li[data-i]"); if (!li) return; from = Number(li.dataset.i); li.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(from)); });
  list.addEventListener("dragover", (e) => { e.preventDefault(); const li = e.target.closest("li[data-i]"); list.querySelectorAll(".drag-over").forEach((x) => x.classList.remove("drag-over")); li?.classList.add("drag-over"); });
  list.addEventListener("dragend", () => { list.querySelectorAll(".dragging,.drag-over").forEach((x) => x.classList.remove("dragging", "drag-over")); from = null; });
  list.addEventListener("drop", (e) => {
    e.preventDefault();
    const li = e.target.closest("li[data-i]");
    if (li == null || from == null) return;
    const r = moveItem(S.queue, from, Number(li.dataset.i), S.index);
    S.queue = r.list; S.index = r.index;
    S.bag = makeBag(S.queue.length, S.index);  // indices moved: start a fresh shuffle cycle
    from = null; renderQueue();
  });
  $("#mp-q-shuffle").addEventListener("click", () => { S.queue = shuffleRest(S.queue, S.index); renderQueue(); toast("Shuffled what's coming up", "info", { ms: 1600 }); });
  $("#mp-q-close")?.addEventListener("click", () => setQueueOpen(false));
  $("#mp-q-clear").addEventListener("click", () => {
    const keep = current();
    S.queue = keep ? [keep] : []; S.index = keep ? 0 : -1; S.bag = []; S.history = [];
    renderQueue();
  });
}

function setQueueOpen(on) {
  S.queueOpen = !!on;
  const q = $("#mp-queue");
  if (q) q.hidden = !S.queueOpen;
  if (S.queueOpen) import("./equalizer.js").then((m) => m.closeEqualizer?.()).catch(() => {});
  renderControls();
  window.dispatchEvent(new CustomEvent("player-change", {
    detail: { key: current()?.key, playing: S.playing, queueOpen: S.queueOpen },
  }));
}

function toggleQueue() { setQueueOpen(!S.queueOpen); }

function toggleShuffle() {
  S.shuffle = !S.shuffle;
  if (S.shuffle) S.bag = makeBag(S.queue.length, S.index);
  renderControls(); savePrefs();
}
function cycleRepeat() { S.repeat = { off: "all", all: "one", one: "off" }[S.repeat]; renderControls(); savePrefs(); }

function toggleWindowPref() {
  if (!current()) return;
  if (hasPicture()) { S.windowWanted = !windowVisible(); S.forcePoster = false; }
  else { S.forcePoster = !windowVisible(); S.windowWanted = true; }
  savePrefs(); syncWindow();
  if (!hasPicture() && S.forcePoster) showWindow();
}

function bindKeys() {
  window.addEventListener("keydown", (e) => {
    if (!current() || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.matches?.("input:not([type=range]), textarea, select, [contenteditable]")) return;
    if (e.target.matches?.("button, a, [role=menuitem]") && (e.code === "Space" || e.key === "Enter")) return;
    const k = e.key.toLowerCase();
    const handled = (fn) => { e.preventDefault(); fn(); };
    if (e.code === "Space" || k === "k") handled(toggle);
    else if (k === "arrowright") handled(() => seekTo(now() + 5));
    else if (k === "arrowleft") handled(() => seekTo(now() - 5));
    else if (k === "l") handled(() => seekTo(now() + 10));
    else if (k === "j") handled(() => seekTo(now() - 10));
    else if (k === "arrowup") handled(() => setVolume(S.volume + 0.05));
    else if (k === "arrowdown") handled(() => setVolume(S.volume - 0.05));
    else if (k === "m") toggleMute();
    else if (k === "f") toggleFullscreen();
    else if (k === "c") cycleSubtitle();
    else if (k === "t") toggleWindowPref();
    else if (k === "n") next();
    else if (k === "p") prev();
    else if (k === "s") toggleShuffle();
    else if (k === "r") cycleRepeat();
    else if (k === "q") toggleQueue();
    else if (k === ">" || k === ".") setRate(RATES[Math.min(RATES.indexOf(S.rate) + 1, RATES.length - 1)]);
    else if (k === "<" || k === ",") setRate(RATES[Math.max(RATES.indexOf(S.rate) - 1, 0)]);
    else if (/^[0-9]$/.test(k)) handled(() => seekTo((Number(k) / 10) * dur()));
  });
}

export function initPlayer() {
  V = $("#mp-v"); A = $("#mp-a");
  loadPrefs();
  initWindow({ onPlayToggle: toggle });
  bindMedia(); bindSeek("mp"); bindSeek("vw"); bindMenus(); bindQueue(); bindKeys();
  renderMenus(); renderControls(); renderQueue();

  $("#mp-play").addEventListener("click", toggle);
  $("#vw-play").addEventListener("click", toggle);
  $("#mp-next").addEventListener("click", next);
  $("#mp-prev").addEventListener("click", prev);
  $("#mp-close").addEventListener("click", closePlayer);
  $("#mp-shuffle").addEventListener("click", toggleShuffle);
  $("#mp-repeat").addEventListener("click", cycleRepeat);
  $("#mp-mute").addEventListener("click", toggleMute);
  $("#mp-vol").addEventListener("input", (e) => setVolume(Number(e.target.value)));
  $("#mp-queue-btn").addEventListener("click", toggleQueue);
  $("#mp-window").addEventListener("click", toggleWindowPref);
  $("#mp-download").addEventListener("click", () => {
    const t = current();
    if (t?.kind === "yt") window.dispatchEvent(new CustomEvent("queue-download", { detail: { urls: [t.url] } }));
  });
  $("#vw-close").addEventListener("click", () => { S.windowWanted = false; S.forcePoster = false; savePrefs(); $("#mp-window").classList.remove("on"); }, true);

  window.addEventListener("player-play", (e) => playTracks(e.detail.tracks, e.detail.start || 0, { shuffle: !!e.detail.shuffle }));
  window.addEventListener("player-enqueue", (e) => enqueue(e.detail.tracks));
}

export const playerState = () => ({ ...S, current: current(), dual: E.dual, master: E.master?.id });

/** What the immersive player mode (stage.js) needs: read-only state plus the transport controls. */
export const playerApi = {
  current, now, dur, seekTo, toggle, next, prev,
  toggleShuffle: () => toggleShuffle(),
  cycleRepeat: () => cycleRepeat(),
  setRate: (r) => setRate(r),
  setSleep: (opt) => setSleep(opt),
  toggleQueue: () => toggleQueue(),
  setQueueOpen: (on) => setQueueOpen(on),
  downloadCurrent: () => {
    const tr = current();
    if (tr?.kind === "yt") window.dispatchEvent(new CustomEvent("queue-download", { detail: { urls: [tr.url] } }));
  },
  state: () => ({
    playing: S.playing, loading: S.loading, shuffle: S.shuffle, repeat: S.repeat,
    rate: S.rate, sleep: !!S.sleep, queueOpen: S.queueOpen, queueLen: S.queue.length,
  }),
  /** The element that carries the sound right now (for the visualiser's analyser). */
  soundElement: () => (E.dual || E.master === A ? A : E.master),
  /** True when the current track has a picture stream (YouTube video / dual HD). */
  hasPicture: () => hasPicture(),
  /** The shared ``<video id="mp-v">`` node (may be moved into the stage). */
  videoElement: () => V,
};
