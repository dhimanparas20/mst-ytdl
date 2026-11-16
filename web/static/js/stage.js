// Immersive "player mode": a full-screen glass stage over the blurred album art, with an animated
// scene (12 visuals + random per track) that reacts to the music, and synced lyrics.
//
// Reads the player through `playerApi` (player.js) and its window events (player-track / player-change);
// never touches the player's internals. The visualiser taps the playing <audio>/<video> with a Web Audio
// analyser — possible because every stream is served from our own origin. Without it (or with
// prefers-reduced-motion) the scenes fall back to a calm, simulated pulse.

import { api } from "./api.js";
import { hookAnalyser, levels, prefersReducedMotion } from "./audiofx.js";
import { playerApi } from "./player.js";
import { getUiPrefs, LYRIC_LANG_OPTIONS, setUiPrefs } from "./prefs.js";
import { activeLine } from "./queue-logic.js";
import { $, $$, esc, fmtDuration, store } from "./util.js";

const PREFS = "mst.stage";
const SCENES = {
  vinyl: "Turntable", aurora: "Aurora", orbit: "Orbit", bars: "Bars",
  ripple: "Ripple", constellation: "Constellation",
  spiral: "Spiral", tunnel: "Tunnel", waves: "Waves", rain: "Rain",
  kaleido: "Kaleido", bloom: "Bloom",
  random: "Random",
};
const RANDOM_POOL = Object.keys(SCENES).filter((k) => k !== "random");
const SCENE_ART = new Set(["bars", "ripple", "constellation", "spiral", "tunnel", "waves", "rain", "kaleido", "bloom"]);

const S = {
  open: false, scene: "vinyl", activeScene: "vinyl", lyricsOn: false, videoOn: false,
  artShape: "circle",  // circle | square — cover banner in every scene
  key: null, lyrics: null, lyricsFor: null, lyricLine: -1, userScrollUntil: 0,
  palette: [[140, 123, 240], [106, 88, 214], [240, 166, 202], [74, 58, 168]],
  seeking: false, raf: 0, bgFlip: false,
};
const VWIN_HOME = "#vwin-body";  // where ``#mp-v`` lives when not on the stage
let cv, g, particles = [], rings = [], stars = [], streaks = [], lastBeat = 0;

// ───────────────────────── open / close ─────────────────────────

export function openStage({ lyrics, video } = {}) {
  if (!playerApi.current()) return;
  // Lyrics: only the Lyrics control passes lyrics:true/false.
  // Plain expand/minimize must re-read the saved preference — never leave a stale true on.
  if (lyrics !== undefined) S.lyricsOn = !!lyrics;
  else S.lyricsOn = readLyricsPref();
  if (video !== undefined) S.videoOn = !!video;
  S.open = true;
  const el = $("#stage");
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("in"));
  document.body.classList.add("stage-open");
  window.dispatchEvent(new CustomEvent("stage-change", { detail: { open: true } }));
  hookAnalyser();  // a user gesture brought us here: allowed to start the AudioContext
  paintLyricsToggle();
  paintVideoToggle();
  paintArtShape();
  onTrack(true);
  loop();
  // Persist scene/video/shape — and the lyrics value we just resolved (so storage stays in sync).
  savePrefs();
}

export function closeStage() {
  if (!S.open) return;
  S.open = false;
  // Persist lyrics on/off before leaving so the next maximize restores it.
  savePrefs();
  import("./equalizer.js").then((m) => m.closeEqualizer?.()).catch(() => {});
  unmountVideo();
  const el = $("#stage");
  el.classList.remove("in", "with-video", "with-lyrics");
  const ly = $("#st-lyrics");
  if (ly) { ly.hidden = true; ly.setAttribute("hidden", ""); }
    document.body.classList.remove("stage-open");
  window.dispatchEvent(new CustomEvent("stage-change", { detail: { open: false } }));
  cancelAnimationFrame(S.raf);
  setTimeout(() => { if (!S.open) el.hidden = true; }, 420);
}

const toggleStage = () => (S.open ? closeStage() : openStage());

function toggleLyrics() {
  if (!S.open) {
    // Lyrics button / Y while docked: open stage with lyrics on.
    return openStage({ lyrics: true });
  }
  S.lyricsOn = !S.lyricsOn;
  savePrefs();  // write BEFORE paint/open races can re-read storage
  paintLyricsToggle();
  if (S.lyricsOn) loadLyrics();
  else {
    const body = $("#ly-body");
    if (body) body.innerHTML = "";
    const src = $("#ly-src");
    if (src) src.textContent = "";
    S.lyrics = null; S.lyricsFor = null; S.lyricLine = -1;
  }
}

function toggleVideo() {
  if (!S.open) return openStage({ video: true });
  S.videoOn = !S.videoOn;
  paintVideoToggle();
  savePrefs();
}

function paintLyricsToggle() {
  const on = !!S.lyricsOn;
  const ly = $("#st-lyrics");
  if (ly) {
    ly.hidden = !on;
    if (on) ly.removeAttribute("hidden");
    else ly.setAttribute("hidden", "");
  }
  $("#stage")?.classList.toggle("with-lyrics", on);
  const btn = $("#st-lyrics-btn");
  btn?.setAttribute("aria-pressed", String(on));
  btn?.classList.toggle("on", on);
  }

function paintVideoToggle() {
  const on = S.open && S.videoOn;
  $("#stage").classList.toggle("with-video", on);
  $("#st-video").hidden = !on;
  $("#st-video-btn").setAttribute("aria-pressed", String(!!S.videoOn));
  $("#st-video-btn").classList.toggle("on", !!S.videoOn);
  if (on) mountVideo();
  else unmountVideo();
  paintVideoEmpty();
}

/** Move the shared ``#mp-v`` into the stage frame so the picture is visible while the floating window is hidden. */
function mountVideo() {
  const v = playerApi.videoElement?.() || $("#mp-v");
  const frame = $("#st-video-frame");
  if (!v || !frame || v.parentElement === frame) return;
  frame.appendChild(v);
}

function unmountVideo() {
  const v = playerApi.videoElement?.() || $("#mp-v");
  const home = $(VWIN_HOME);
  if (!v || !home || v.parentElement === home) return;
  // keep the empty-state sibling; put the video back as the first child of the floating window body
  home.insertBefore(v, home.firstChild);
}

function paintVideoEmpty() {
  const empty = $("#st-video-empty");
  if (!empty) return;
  const show = S.open && S.videoOn && !playerApi.hasPicture?.();
  empty.hidden = !show;
}

function toggleArtShape() {
  S.artShape = S.artShape === "square" ? "circle" : "square";
  paintArtShape();
  savePrefs();
}

function paintArtShape() {
  const shape = S.artShape === "square" ? "square" : "circle";
  S.artShape = shape;
  const el = $("#stage");
  el?.classList.toggle("art-circle", shape === "circle");
  el?.classList.toggle("art-square", shape === "square");
  const btn = $("#st-art-shape");
  btn?.setAttribute("aria-pressed", String(shape === "circle"));
  btn?.classList.toggle("on", shape === "circle");
  const ico = $("#st-art-shape-ico");
  if (ico) ico.className = `bi bi-${shape === "circle" ? "circle" : "square"}`;
  const lab = $("#st-art-shape-label");
  if (lab) lab.textContent = shape === "circle" ? "Circle" : "Square";
  btn?.setAttribute("title", `Cover shape: ${shape} (click to switch)`);
}

function savePrefs() {
  store(PREFS, { scene: S.scene, lyricsOn: !!S.lyricsOn, videoOn: !!S.videoOn, artShape: S.artShape });
}

/** Last lyrics on/off from localStorage (source of truth across minimize/maximize). */
function readLyricsPref() {
  try {
    const p = store(PREFS) || {};
    return !!p.lyricsOn;
  } catch {
    return !!S.lyricsOn;
  }
}

// ───────────────────────── track / scene ─────────────────────────

function setScene(scene, { persist = true } = {}) {
  S.scene = SCENES[scene] ? scene : "vinyl";
  S.activeScene = S.scene === "random" ? RANDOM_POOL[Math.floor(Math.random() * RANDOM_POOL.length)] : S.scene;
  $("#stage").dataset.scene = S.activeScene;
  $("#st-scene-name").textContent = S.scene === "random" ? `Random · ${SCENES[S.activeScene]}` : SCENES[S.scene];
  $$("#st-scene-menu [data-scene]").forEach((b) => b.classList.toggle("active", b.dataset.scene === S.scene));
  particles = []; rings = []; stars = []; streaks = [];
  if (persist) savePrefs();
}

function onTrack(force = false) {
  const t = playerApi.current();
  if (!t || (!force && t.key === S.key)) return;
  const changed = t.key !== S.key;
  S.key = t.key;
  if (changed && S.scene === "random") setScene("random", { persist: false });
  $("#st-title").textContent = t.title || "";
  $("#st-artist").textContent = t.artist || "";
  $("#st-src").textContent = t.kind === "yt" ? "YouTube" : "Library";
  const art = t.thumb || "";
  for (const id of ["#st-label", "#st-orbit-art", "#st-aurora-art", "#st-scene-art"]) {
    const img = $(id);
    img.hidden = !art;
    if (art && img.getAttribute("src") !== art) img.src = art;
  }
  $("#stage").classList.toggle("no-art", !art);
  // cross-fade the blurred background
  S.bgFlip = !S.bgFlip;
  const [inEl, outEl] = S.bgFlip ? [$("#st-bg-a"), $("#st-bg-b")] : [$("#st-bg-b"), $("#st-bg-a")];
  if (art) { inEl.src = art; inEl.classList.add("on"); } else inEl.classList.remove("on");
  outEl.classList.remove("on");
  samplePalette(art);
  S.lyrics = null; S.lyricsFor = null; S.lyricLine = -1;
  $("#ly-body").innerHTML = "";
  $("#ly-src").textContent = "";
  if (S.lyricsOn) loadLyrics();
  if (S.videoOn) { mountVideo(); paintVideoEmpty(); }
  paintTransport();
}

/** Dominant colours of the cover (when the image allows reading its pixels) tint the scenes. */
function samplePalette(src) {
  if (!src) return;
  const img = new Image();
  img.crossOrigin = "anonymous";
  img.onload = () => {
    try {
      const c = document.createElement("canvas");
      c.width = c.height = 12;
      const x = c.getContext("2d", { willReadFrequently: true });
      x.drawImage(img, 0, 0, 12, 12);
      const px = x.getImageData(0, 0, 12, 12).data;
      const cols = [];
      for (let i = 0; i < px.length; i += 4) {
        const [r, gg, b] = [px[i], px[i + 1], px[i + 2]];
        const max = Math.max(r, gg, b), min = Math.min(r, gg, b);
        if (max - min > 28 && max > 60) cols.push([r, gg, b, max - min]);  // colourful, not grey / black
      }
      cols.sort((a, b) => b[3] - a[3]);
      if (cols.length >= 2) S.palette = [0, 1, 2, 3].map((k) => cols[Math.min(cols.length - 1, k * Math.floor(cols.length / 4))].slice(0, 3));
    } catch { /* cross-origin cover without CORS: keep the theme palette */ }
  };
  img.src = src;
}

// ───────────────────────── lyrics ─────────────────────────

function lyricsLang() {
  return getUiPrefs().lyricsLang || "auto";
}

function paintLyLang() {
  const sel = $("#ly-lang");
  if (!sel) return;
  if (!sel.options.length) {
    sel.innerHTML = LYRIC_LANG_OPTIONS.map(([c, label]) => `<option value="${c}">${label}</option>`).join("");
  }
  sel.value = lyricsLang();
}

async function loadLyrics({ force = false } = {}) {
  const t = playerApi.current();
  const lang = lyricsLang();
  const cacheKey = t ? `${t.key}|${lang}` : null;
  if (!t || (!force && S.lyricsFor === cacheKey)) return;
  S.lyricsFor = cacheKey;
  paintLyLang();
  const body = $("#ly-body");
  body.innerHTML = `<div class="ly-wait"><span class="eq big"><i></i><i></i><i></i><i></i><i></i></span><span>${lang === "auto" ? "Finding lyrics…" : "Finding / translating lyrics…"}</span></div>`;
  let res;
  try {
    res = await api.lyrics(
      t.title,
      t.artist && t.artist !== "Library" ? t.artist : undefined,
      playerApi.dur() || t.duration,
      lang,
    );
  } catch (e) { res = { found: false, message: e.detail || e.message, synced: [] }; }
  if (playerApi.current()?.key !== t.key || lyricsLang() !== lang) return;  // track or language changed
  S.lyrics = res;
  S.lyricLine = -1;
  if (!res.found) {
    body.innerHTML = `<div class="ly-none"><i class="bi bi-music-note-list"></i><b>No lyrics</b><span>${esc(res.message || "Nothing found for this track.")}</span></div>`;
  } else if (res.instrumental && !res.synced.length && !res.plain) {
    body.innerHTML = `<div class="ly-none"><i class="bi bi-soundwave"></i><b>Instrumental</b></div>`;
  } else if (res.synced.length) {
    body.innerHTML = `<div class="ly-pad"></div>` + res.synced.map((l, i) =>
      `<p class="ly-line" data-i="${i}" data-t="${l.t}">${esc(l.text) || "♪"}</p>`).join("") + `<div class="ly-pad"></div>`;
  } else {
    body.innerHTML = `<div class="ly-plain">${esc(res.plain)}</div>`;
  }
  const bits = [];
  if (res.found) {
    bits.push(`${res.synced.length ? "Synced" : "Plain"} lyrics · ${esc(res.source)}`);
    if (res.translated) bits.push("translated");
    else if (res.language) bits.push(esc(res.language));
    if (res.track) bits.push(`${esc(res.artist)} — ${esc(res.track)}`);
    if (res.message) bits.push(esc(res.message));
  }
  $("#ly-src").innerHTML = bits.join(" · ");
  body.scrollTop = 0;
}

function syncLyrics(t) {
  const L = S.lyrics;
  if (!S.lyricsOn || !L?.synced?.length) return;
  const i = activeLine(L.synced, t);
  if (i === S.lyricLine) return;
  const body = $("#ly-body");
  body.querySelector(".ly-line.on")?.classList.remove("on");
  body.querySelectorAll(".ly-line.past").forEach((el) => { if (+el.dataset.i >= i) el.classList.remove("past"); });
  S.lyricLine = i;
  if (i < 0) return;
  const el = body.querySelector(`.ly-line[data-i="${i}"]`);
  if (!el) return;
  el.classList.add("on");
  for (let k = Math.max(0, i - 40); k < i; k++) body.querySelector(`.ly-line[data-i="${k}"]`)?.classList.add("past");
  if (Date.now() > S.userScrollUntil) {
    body.scrollTo({ top: el.offsetTop - body.clientHeight / 2 + el.clientHeight / 2, behavior: prefersReducedMotion() ? "auto" : "smooth" });
  }
}

// ───────────────────────── render loop ─────────────────────────

function loop() {
  cancelAnimationFrame(S.raf);
  const frame = (time) => {
    if (!S.open) return;
    const lv = levels(time);
    const st = $("#stage");
    st.style.setProperty("--beat", lv.bass.toFixed(3));
    st.style.setProperty("--mid", lv.mid.toFixed(3));
    const cur = playerApi.now(), total = playerApi.dur();
    if (!S.seeking) {
      $("#st-seek").value = total ? Math.round((cur / total) * 1000) : 0;
      $("#st-seek").style.setProperty("--p", `${total ? (cur / total) * 100 : 0}%`);
      $("#st-cur").textContent = fmtDuration(cur) || "0:00";
    }
    $("#st-dur").textContent = total ? fmtDuration(total) : "0:00";
    syncLyrics(cur);
    if (!prefersReducedMotion()) draw(time, lv);
    S.raf = requestAnimationFrame(frame);
  };
  S.raf = requestAnimationFrame(frame);
}

function fitCanvas() {
  const dpr = Math.min(devicePixelRatio || 1, 2);
  const w = innerWidth, h = innerHeight;
  // both sides: entering fullscreen changes only the HEIGHT — a stale height stretches the drawing (oval ring)
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [w, h];
}

const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

function draw(time, lv) {
  const [w, h] = fitCanvas();
  g.clearRect(0, 0, w, h);
  const scene = S.activeScene;
  const P = S.palette;
  const visual = $(".stage-visual").getBoundingClientRect();
  const cx = visual.left + visual.width / 2, cy = visual.top + visual.height / 2;

  if (scene === "aurora") {  // drifting light curtains in the cover's colours
    g.globalCompositeOperation = "lighter";
    for (let i = 0; i < 4; i++) {
      const t = time / (9000 + i * 2300);
      const x = w * (0.5 + 0.38 * Math.sin(t * 2 + i * 1.7));
      const y = h * (0.45 + 0.3 * Math.cos(t * 1.6 + i * 2.1));
      const r = Math.max(w, h) * (0.32 + 0.1 * Math.sin(t * 3 + i)) * (1 + lv.bass * 0.35);
      const grad = g.createRadialGradient(x, y, 0, x, y, r);
      grad.addColorStop(0, rgba(P[i % P.length], 0.22 + lv.mid * 0.25));
      grad.addColorStop(1, rgba(P[i % P.length], 0));
      g.fillStyle = grad;
      g.fillRect(0, 0, w, h);
    }
    g.globalCompositeOperation = "source-over";
  }

  if (scene === "orbit" && lv) {  // radial spectrum around the cover
    const art = $("#st-orbit-art").getBoundingClientRect();
    const R = art.width / 2 + 10;
    const bars = 96;
    g.save();
    g.translate(art.left + art.width / 2, art.top + art.height / 2);
    for (let i = 0; i < bars; i++) {
      const bin = lv.spec ? lv.spec[Math.floor((i < bars / 2 ? i : bars - i) * 0.9) + 2] / 255
        : (Math.sin(time / 180 + i * 0.5) * 0.5 + 0.5) * lv.mid * 1.4;
      const len = 6 + bin * R * 0.55;
      const a = (i / bars) * Math.PI * 2 - Math.PI / 2;
      g.strokeStyle = rgba(P[i % 2 ? 0 : 2], 0.35 + bin * 0.6);
      g.lineWidth = Math.max(2, (Math.PI * 2 * R) / bars - 3);
      g.lineCap = "round";
      g.beginPath();
      g.moveTo(Math.cos(a) * R, Math.sin(a) * R);
      g.lineTo(Math.cos(a) * (R + len), Math.sin(a) * (R + len));
      g.stroke();
    }
    g.restore();
  }

  if (scene === "bars") {  // mirrored spectrum: lows in the middle, highs at the edges, reflected above / below
    const N = Math.max(24, Math.min(64, Math.floor(w / 22))), gap = 4;
    const bw = w / 2 / N - gap, maxH = h * 0.3;
    for (let i = 0; i < N; i++) {
      const bin = lv.spec ? lv.spec[1 + Math.floor((i / N) ** 1.4 * 90)] / 255
        : (Math.sin(time / 220 - i * 0.35) * 0.5 + 0.5) * lv.mid * 1.5 * (1 - i / N * 0.6);
      const bh = 4 + bin * maxH;
      g.fillStyle = rgba(P[i % 2 ? 0 : 2], 0.3 + bin * 0.6);
      for (const side of [1, -1]) {
        const x = w / 2 + side * (i * (bw + gap) + gap / 2) - (side < 0 ? bw : 0);
        g.beginPath();
        g.roundRect(x, cy - bh, bw, bh * 2, Math.min(bw / 2, 6));
        g.fill();
      }
    }
  }

  if (scene === "ripple") {  // a ring leaves the cover on every beat (and slowly while it is quiet)
    const art = $("#st-scene-art").getBoundingClientRect();
    const rx = art.left + art.width / 2, ry = art.top + art.height / 2, R0 = art.width / 2;
    if ((lv.bass > 0.5 && time - lastBeat > 260) || time - lastBeat > 1800) { rings.push({ born: time, e: Math.min(1, 0.4 + lv.bass) }); lastBeat = time; }
    const life = 2400, reach = Math.hypot(w, h) * 0.42;
    rings = rings.filter((r) => time - r.born < life);
    g.lineWidth = 3;
    for (const r of rings) {
      const k = (time - r.born) / life, ease = 1 - (1 - k) ** 3;
      g.strokeStyle = rgba(P[Math.floor(r.born / 400) % P.length], (1 - k) * (0.25 + r.e * 0.5));
      g.beginPath();
      g.arc(rx, ry, R0 + 8 + ease * reach, 0, Math.PI * 2);
      g.stroke();
    }
  }

  if (scene === "constellation") {  // drifting stars joined when close; energy makes them flare and the links brighter
    const want = Math.min(90, Math.floor((w * h) / 16000)), reach = Math.min(160, Math.max(100, w / 9));
    while (stars.length < want) stars.push({ x: Math.random() * w, y: Math.random() * h, vx: (Math.random() - 0.5) * 0.5, vy: (Math.random() - 0.5) * 0.5, c: stars.length % 4, bin: 2 + Math.floor(Math.random() * 60) });
    const speed = 0.6 + lv.bass * 2.2;
    for (const s of stars) {
      s.x = (s.x + s.vx * speed + w) % w;
      s.y = (s.y + s.vy * speed + h) % h;
      s.e = lv.spec ? lv.spec[s.bin] / 255 : lv.mid * (0.6 + 0.4 * Math.sin(time / 500 + s.bin));
    }
    g.lineWidth = 1;
    for (let i = 0; i < stars.length; i++) {
      const a = stars[i];
      for (let j = i + 1; j < stars.length; j++) {
        const b = stars[j], d = Math.hypot(a.x - b.x, a.y - b.y);
        if (d > reach) continue;
        g.strokeStyle = rgba(P[a.c], (1 - d / reach) * (0.12 + (a.e + b.e) * 0.5 + lv.bass * 0.25));
        g.beginPath();
        g.moveTo(a.x, a.y);
        g.lineTo(b.x, b.y);
        g.stroke();
      }
    }
    for (const s of stars) {
      g.fillStyle = rgba(P[s.c], Math.min(1, 0.45 + s.e * 0.7));
      g.beginPath();
      g.arc(s.x, s.y, 1.6 + s.e * 4.5, 0, Math.PI * 2);
      g.fill();
    }
  }

  if (scene === "spiral") {  // spinning arms from the cover — bass winds them tighter
    const art = $("#st-scene-art").getBoundingClientRect();
    const rx = art.left + art.width / 2, ry = art.top + art.height / 2;
    const arms = 5, turns = 3.2, spin = time / (2800 - lv.bass * 900);
    g.lineCap = "round";
    for (let a = 0; a < arms; a++) {
      g.beginPath();
      for (let i = 0; i <= 90; i++) {
        const t = i / 90;
        const ang = spin + a * ((Math.PI * 2) / arms) + t * Math.PI * 2 * turns;
        const rad = art.width * 0.28 + t * Math.min(w, h) * (0.42 + lv.mid * 0.12);
        const x = rx + Math.cos(ang) * rad, y = ry + Math.sin(ang) * rad;
        i ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      g.strokeStyle = rgba(P[a % P.length], 0.22 + lv.bass * 0.45);
      g.lineWidth = 2.5 + lv.high * 4;
      g.stroke();
    }
  }

  if (scene === "tunnel") {  // hyperspace rings rushing toward the cover
    const art = $("#st-scene-art").getBoundingClientRect();
    const rx = art.left + art.width / 2, ry = art.top + art.height / 2, R0 = art.width / 2;
    if (rings.length < 14) {
      for (let i = rings.length; i < 14; i++) rings.push({ z: i / 14, hue: i % P.length });
    }
    const rush = 0.004 + lv.bass * 0.018 + lv.mid * 0.006;
    for (const r of rings) {
      r.z += rush;
      if (r.z > 1) { r.z -= 1; r.hue = (r.hue + 1) % P.length; }
      const ease = r.z * r.z;
      const rad = R0 + 6 + ease * Math.hypot(w, h) * 0.55;
      g.strokeStyle = rgba(P[r.hue], (1 - ease) * (0.15 + lv.bass * 0.5));
      g.lineWidth = 2 + (1 - ease) * 5;
      g.beginPath();
      g.arc(rx, ry, rad, 0, Math.PI * 2);
      g.stroke();
    }
  }

  if (scene === "waves") {  // layered sine seas across the stage
    const layers = 5;
    for (let L = 0; L < layers; L++) {
      const amp = (18 + L * 10) * (0.55 + lv.bass * 1.4 + lv.mid * 0.6);
      const y0 = h * (0.35 + L * 0.1);
      const speed = time / (900 + L * 280);
      g.beginPath();
      for (let x = 0; x <= w; x += 8) {
        const y = y0
          + Math.sin(x * 0.012 + speed + L) * amp
          + Math.sin(x * 0.027 - speed * 1.3) * amp * 0.35;
        x ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      g.lineTo(w, h); g.lineTo(0, h); g.closePath();
      g.fillStyle = rgba(P[L % P.length], 0.08 + lv.mid * 0.1);
      g.fill();
      g.strokeStyle = rgba(P[L % P.length], 0.25 + lv.high * 0.35);
      g.lineWidth = 1.5;
      g.beginPath();
      for (let x = 0; x <= w; x += 8) {
        const y = y0
          + Math.sin(x * 0.012 + speed + L) * amp
          + Math.sin(x * 0.027 - speed * 1.3) * amp * 0.35;
        x ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      g.stroke();
    }
  }

  if (scene === "rain") {  // audio-reactive meteor rain
    const want = Math.min(110, Math.floor(w / 10));
    while (streaks.length < want) {
      streaks.push({
        x: Math.random() * w, y: Math.random() * h, len: 12 + Math.random() * 28,
        v: 4 + Math.random() * 9, c: streaks.length % 4, bin: 4 + Math.floor(Math.random() * 70),
      });
    }
    g.lineCap = "round";
    for (const s of streaks) {
      const e = lv.spec ? lv.spec[s.bin] / 255 : lv.high * (0.5 + 0.5 * Math.sin(time / 400 + s.bin));
      s.y += s.v * (0.7 + e * 2.4 + lv.bass);
      s.x += 0.4 + e * 1.2;
      if (s.y > h + 40 || s.x > w + 40) { s.y = -30; s.x = Math.random() * w; }
      g.strokeStyle = rgba(P[s.c], 0.2 + e * 0.75);
      g.lineWidth = 1 + e * 2.5;
      g.beginPath();
      g.moveTo(s.x, s.y);
      g.lineTo(s.x - s.len * 0.35, s.y - s.len);
      g.stroke();
    }
  }

  if (scene === "kaleido") {  // mirrored spectrum wedges around the cover
    const art = $("#st-scene-art").getBoundingClientRect();
    const rx = art.left + art.width / 2, ry = art.top + art.height / 2;
    const slices = 8, bars = 18;
    const R0 = art.width / 2 + 8, R1 = Math.min(w, h) * 0.42;
    g.save();
    g.translate(rx, ry);
    g.rotate(time / 8000);
    for (let s = 0; s < slices; s++) {
      g.save();
      g.rotate((s / slices) * Math.PI * 2);
      if (s % 2) g.scale(1, -1);
      for (let i = 0; i < bars; i++) {
        const bin = lv.spec ? lv.spec[2 + Math.floor((i / bars) ** 1.2 * 80)] / 255
          : (Math.sin(time / 200 + i * 0.4 + s) * 0.5 + 0.5) * lv.mid;
        const t0 = (i / bars) * (Math.PI / slices);
        const t1 = ((i + 0.85) / bars) * (Math.PI / slices);
        const inner = R0, outer = R0 + (R1 - R0) * (0.15 + bin * 0.85);
        g.fillStyle = rgba(P[(s + i) % P.length], 0.18 + bin * 0.55);
        g.beginPath();
        g.arc(0, 0, outer, t0, t1);
        g.arc(0, 0, inner, t1, t0, true);
        g.closePath();
        g.fill();
      }
      g.restore();
    }
    g.restore();
  }

  if (scene === "bloom") {  // flower petals / light rays blooming from the cover
    const art = $("#st-scene-art").getBoundingClientRect();
    const rx = art.left + art.width / 2, ry = art.top + art.height / 2;
    const petals = 12, spin = time / 5000;
    const R0 = art.width / 2, reach = Math.min(w, h) * (0.28 + lv.bass * 0.12);
    g.save();
    g.translate(rx, ry);
    for (let i = 0; i < petals; i++) {
      const ang = spin + (i / petals) * Math.PI * 2;
      const bin = lv.spec ? lv.spec[3 + i * 5] / 255 : lv.mid * (0.5 + 0.5 * Math.sin(time / 450 + i));
      const len = reach * (0.55 + bin * 0.9);
      const width = 0.18 + bin * 0.22;
      g.save();
      g.rotate(ang);
      const grad = g.createLinearGradient(0, 0, len, 0);
      grad.addColorStop(0, rgba(P[i % P.length], 0.05));
      grad.addColorStop(0.45, rgba(P[i % P.length], 0.35 + bin * 0.4));
      grad.addColorStop(1, rgba(P[i % P.length], 0));
      g.fillStyle = grad;
      g.beginPath();
      g.moveTo(R0 * 0.6, 0);
      g.quadraticCurveTo(len * 0.55, -len * width, len, 0);
      g.quadraticCurveTo(len * 0.55, len * width, R0 * 0.6, 0);
      g.fill();
      g.restore();
    }
    g.restore();
  }

  // floating light motes (all scenes but the constellation): rise faster with the beat
  const want = scene === "constellation" || scene === "rain" ? 0 : scene === "vinyl" ? 38 : scene === "bars" ? 24 : SCENE_ART.has(scene) ? 40 : 60;
  while (particles.length < want) particles.push(newMote(w, h, true));
  for (const p of particles) {
    p.y -= p.v * (0.6 + lv.bass * 2.2);
    p.x += Math.sin((time + p.seed) / 1400) * 0.25;
    if (p.y < -20) Object.assign(p, newMote(w, h, false));
    const a = p.a * (0.55 + lv.high * 1.6);
    const grad = g.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r * 3);
    grad.addColorStop(0, rgba(P[p.c], Math.min(0.9, a)));
    grad.addColorStop(1, rgba(P[p.c], 0));
    g.fillStyle = grad;
    g.beginPath();
    g.arc(p.x, p.y, p.r * 3, 0, Math.PI * 2);
    g.fill();
  }
  if (scene === "vinyl") {  // soft halo behind the record
    const r = Math.min(visual.width, visual.height) * 0.42 * (1 + lv.bass * 0.12);
    const grad = g.createRadialGradient(cx, cy, r * 0.6, cx, cy, r * 1.35);
    grad.addColorStop(0, rgba(P[0], 0.18 + lv.bass * 0.3));
    grad.addColorStop(1, rgba(P[0], 0));
    g.fillStyle = grad;
    g.fillRect(0, 0, w, h);
  }
}

function newMote(w, h, anywhere) {
  return {
    x: Math.random() * w, y: anywhere ? Math.random() * h : h + 20, r: 1 + Math.random() * 2.6,
    v: 0.2 + Math.random() * 0.7, a: 0.15 + Math.random() * 0.4, c: Math.floor(Math.random() * 4), seed: Math.random() * 1e4,
  };
}

// ───────────────────────── transport ─────────────────────────

function paintTransport() {
  const s = playerApi.state();
  $("#stage").classList.toggle("playing", s.playing);
  $("#st-play i").className = `bi bi-${s.playing ? "pause-fill" : "play-fill"}`;
  $("#st-play").setAttribute("aria-label", s.playing ? "Pause" : "Play");
  $("#st-shuffle").classList.toggle("on", s.shuffle);
  $("#st-repeat").classList.toggle("on", s.repeat !== "off");
  $("#st-repeat i").className = `bi bi-${s.repeat === "one" ? "repeat-1" : "repeat"}`;
  const rate = $("#st-rate");
  if (rate && s.rate != null) rate.textContent = `${s.rate}×`;
  $("#st-sleep")?.classList.toggle("on", !!s.sleep);
  $("#st-queue-btn")?.classList.toggle("on", !!s.queueOpen);
  const qc = $("#st-queue-count");
  if (qc && s.queueLen != null) qc.textContent = String(s.queueLen);
  const t = playerApi.current();
  const dl = $("#st-download");
  if (dl) dl.hidden = t?.kind !== "yt";
}

// ───────────────────────── init ─────────────────────────

export function initStage() {
  cv = $("#stage-fx");
  g = cv.getContext("2d");
  const p = store(PREFS) || {};
  S.lyricsOn = !!p.lyricsOn;
  S.videoOn = !!p.videoOn;
  S.artShape = p.artShape === "square" ? "square" : "circle";
  setScene(p.scene || "vinyl", { persist: false });
  paintArtShape();

  $("#mp-stage").addEventListener("click", () => openStage());  // respects saved lyricsOn
  $("#st-close").addEventListener("click", closeStage);
  $("#st-lyrics-btn").addEventListener("click", toggleLyrics);
  $("#st-art-shape")?.addEventListener("click", toggleArtShape);
  $("#st-video-btn").addEventListener("click", toggleVideo);
  $("#st-video-frame")?.addEventListener("click", () => {
    if (!S.videoOn) return;
    playerApi.toggle();
    hookAnalyser();
  });
  $("#st-scene-menu").addEventListener("click", (e) => { const b = e.target.closest("[data-scene]"); if (b) setScene(b.dataset.scene); });
  $("#st-play").addEventListener("click", () => { playerApi.toggle(); hookAnalyser(); });
  $("#st-next").addEventListener("click", () => playerApi.next());
  $("#st-prev").addEventListener("click", () => playerApi.prev());
  $("#st-shuffle").addEventListener("click", () => { playerApi.toggleShuffle(); paintTransport(); });
  $("#st-repeat").addEventListener("click", () => { playerApi.cycleRepeat(); paintTransport(); });
  // EQ is bound in equalizer.js (#st-eq-btn); queue opens the shared panel above the stage.
  $("#st-queue-btn")?.addEventListener("click", () => {
    playerApi.toggleQueue();
    paintTransport();
  });
  $("#st-download")?.addEventListener("click", () => playerApi.downloadCurrent());
  const seek = $("#st-seek");
  seek.addEventListener("input", () => {
    S.seeking = true;
    const t = (seek.value / 1000) * playerApi.dur();
    $("#st-cur").textContent = fmtDuration(t) || "0:00";
    seek.style.setProperty("--p", `${seek.value / 10}%`);
  });
  seek.addEventListener("change", () => { playerApi.seekTo((seek.value / 1000) * playerApi.dur()); S.seeking = false; });
  $("#ly-body").addEventListener("click", (e) => {
    const line = e.target.closest(".ly-line");
    if (line) { playerApi.seekTo(Number(line.dataset.t)); S.userScrollUntil = 0; }
  });
  for (const ev of ["wheel", "touchmove"]) $("#ly-body").addEventListener(ev, () => { S.userScrollUntil = Date.now() + 4000; }, { passive: true });
  paintLyLang();
  $("#ly-lang")?.addEventListener("change", () => {
    setUiPrefs({ lyricsLang: $("#ly-lang").value || "auto" });
    if (S.lyricsOn) loadLyrics({ force: true });
  });
  window.addEventListener("ui-prefs", () => {
    paintLyLang();
    if (S.open && S.lyricsOn) loadLyrics({ force: true });
  });

  window.addEventListener("player-track", () => { if (S.open) { onTrack(); hookAnalyser(); } });
  window.addEventListener("player-change", (e) => {
    if (!e.detail.key && S.open) return closeStage();  // the player was closed
    if (S.open) {
      paintTransport();
      paintVideoEmpty();
      if (e.detail.playing) hookAnalyser();
    }
  });
  window.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || e.target.matches?.("input:not([type=range]), textarea, select, [contenteditable]")) return;
    const k = e.key.toLowerCase();
    if (k === "escape" && S.open) { e.preventDefault(); closeStage(); }
    else if (k === "v" && playerApi.current()) { e.preventDefault(); toggleStage(); }
    else if (k === "y" && S.open) { e.preventDefault(); toggleLyrics(); }  // lyrics control is stage-only
    else if (k === "p" && S.open) { e.preventDefault(); toggleVideo(); }
  });
  matchMedia("(prefers-reduced-motion: reduce)").addEventListener?.("change", () => { if (S.open) loop(); });
}
