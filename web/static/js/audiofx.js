// Shared Web Audio graph for the player: equalizer → surround → analyser → speakers.
//
// A media element may be wired with ``createMediaElementSource`` only once — afterwards its
// sound always flows through this graph — so stage, ambient and the EQ UI must share this module.
// Never start the context while suspended: that would silence the player (see AGENTS §10.26).

import { playerApi } from "./player.js";
import { store } from "./util.js";

const KEY = "mst.eq";
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");

/** Classic 10-band graphic EQ centre frequencies (Hz). */
export const EQ_BANDS = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];

export const EQ_PRESETS = {
  flat: { label: "Flat", preamp: 0, bass: 0, mid: 0, treble: 0, surround: 0, bands: EQ_BANDS.map(() => 0) },
  bass: { label: "Bass boost", preamp: -1, bass: 7, mid: 0, treble: -1, surround: 0.15, bands: [6, 5, 4, 2, 0, 0, 0, 0, 0, 0] },
  treble: { label: "Treble boost", preamp: -1, bass: -1, mid: 1, treble: 6, surround: 0.1, bands: [0, 0, 0, 0, 0, 1, 2, 4, 5, 6] },
  vocal: { label: "Vocal", preamp: 0, bass: -2, mid: 5, treble: 2, surround: 0.2, bands: [-2, -1, 0, 2, 4, 5, 3, 1, 0, -1] },
  electronic: { label: "Electronic", preamp: -1, bass: 5, mid: -1, treble: 4, surround: 0.35, bands: [5, 4, 2, 0, -2, 0, 2, 3, 4, 3] },
  rock: { label: "Rock", preamp: -1, bass: 4, mid: 1, treble: 3, surround: 0.2, bands: [4, 3, 1, 0, -1, 1, 2, 3, 3, 2] },
  soft: { label: "Soft", preamp: 0, bass: 2, mid: -1, treble: -2, surround: 0.25, bands: [2, 2, 1, 0, -1, -1, -1, -2, -2, -2] },
  loudness: { label: "Loudness", preamp: -2, bass: 5, mid: -2, treble: 4, surround: 0.1, bands: [5, 4, 2, 0, -2, -2, 0, 2, 4, 5] },
  cinema: { label: "Cinema", preamp: -1, bass: 4, mid: 0, treble: 2, surround: 0.55, bands: [4, 3, 2, 0, 0, 0, 1, 2, 2, 1] },
};

const DEFAULTS = {
  enabled: false,
  preset: "flat",
  preamp: 0,       // -12..+12 dB
  bass: 0,         // -12..+12 dB  (low shelf ~120 Hz)
  mid: 0,          // -12..+12 dB  (peaking ~1 kHz)
  treble: 0,       // -12..+12 dB  (high shelf ~4 kHz)
  surround: 0,     // 0..1 stereo width / space
  bands: EQ_BANDS.map(() => 0),
};

const G = {
  ctx: null,
  input: null,       // GainNode — entry after MediaElementSource
  bands: [],         // BiquadFilterNode peaking
  bass: null, mid: null, treble: null,
  splitter: null, delayL: null, delayR: null, merger: null,
  wet: null, dry: null, mix: null,  // surround wet/dry
  analyser: null, data: null,
  master: null,
  sources: new WeakMap(),
  built: false,
};

let eq = { ...DEFAULTS, bands: [...DEFAULTS.bands] };

function clamp(n, a, b) { return Math.min(b, Math.max(a, n)); }

function loadEq() {
  const saved = store(KEY);
  if (!saved || typeof saved !== "object") return;
  eq = {
    ...DEFAULTS,
    ...saved,
    bands: Array.isArray(saved.bands) && saved.bands.length === EQ_BANDS.length
      ? saved.bands.map((v) => clamp(Number(v) || 0, -12, 12))
      : [...DEFAULTS.bands],
    preamp: clamp(Number(saved.preamp) || 0, -12, 12),
    bass: clamp(Number(saved.bass) || 0, -12, 12),
    mid: clamp(Number(saved.mid) || 0, -12, 12),
    treble: clamp(Number(saved.treble) || 0, -12, 12),
    surround: clamp(Number(saved.surround) || 0, 0, 1),
    enabled: !!saved.enabled,
    preset: typeof saved.preset === "string" ? saved.preset : "flat",
  };
}

function persist() { store(KEY, eq); }

function buildGraph(ctx) {
  if (G.built) return;
  G.input = ctx.createGain();
  G.bands = EQ_BANDS.map((hz) => {
    const f = ctx.createBiquadFilter();
    f.type = "peaking";
    f.frequency.value = hz;
    f.Q.value = 1.1;
    f.gain.value = 0;
    return f;
  });
  G.bass = ctx.createBiquadFilter();
  G.bass.type = "lowshelf";
  G.bass.frequency.value = 120;
  G.mid = ctx.createBiquadFilter();
  G.mid.type = "peaking";
  G.mid.frequency.value = 1000;
  G.mid.Q.value = 0.9;
  G.treble = ctx.createBiquadFilter();
  G.treble.type = "highshelf";
  G.treble.frequency.value = 4000;

  // Surround = dry path + stereo Haas delay wet path, mixed by ``surround`` 0..1
  G.dry = ctx.createGain();
  G.wet = ctx.createGain();
  G.mix = ctx.createGain();
  G.splitter = ctx.createChannelSplitter(2);
  G.delayL = ctx.createDelay(0.05);
  G.delayR = ctx.createDelay(0.05);
  G.merger = ctx.createChannelMerger(2);

  G.analyser = ctx.createAnalyser();
  G.analyser.fftSize = 256;
  G.analyser.smoothingTimeConstant = 0.82;
  G.data = new Uint8Array(G.analyser.frequencyBinCount);
  G.master = ctx.createGain();

  // Chain: input → bands… → bass → mid → treble → (dry + wet surround) → mix → analyser → master → dest
  let node = G.input;
  for (const b of G.bands) { node.connect(b); node = b; }
  node.connect(G.bass);
  G.bass.connect(G.mid);
  G.mid.connect(G.treble);

  G.treble.connect(G.dry);
  G.treble.connect(G.splitter);
  G.splitter.connect(G.delayL, 0);
  G.splitter.connect(G.delayR, 1);
  G.delayL.connect(G.merger, 0, 0);
  G.delayR.connect(G.merger, 0, 1);
  G.merger.connect(G.wet);

  G.dry.connect(G.mix);
  G.wet.connect(G.mix);
  G.mix.connect(G.analyser);
  G.analyser.connect(G.master);
  G.master.connect(ctx.destination);

  G.built = true;
  applyEqToGraph();
}

function applyEqToGraph() {
  if (!G.built) return;
  const on = !!eq.enabled;
  G.input.gain.value = on ? Math.pow(10, eq.preamp / 20) : 1;
  G.bands.forEach((f, i) => { f.gain.value = on ? eq.bands[i] : 0; });
  G.bass.gain.value = on ? eq.bass : 0;
  G.mid.gain.value = on ? eq.mid : 0;
  G.treble.gain.value = on ? eq.treble : 0;
  const w = on ? clamp(eq.surround, 0, 1) : 0;
  G.dry.gain.value = 1 - w * 0.55;
  G.wet.gain.value = w * 0.85;
  G.delayL.delayTime.value = 0.004 + w * 0.014;
  G.delayR.delayTime.value = 0.008 + w * 0.022;
  G.master.gain.value = 1;
}

/** Current EQ settings (copy). */
export function getEq() {
  return { ...eq, bands: [...eq.bands] };
}

/**
 * Patch EQ settings and push them onto the live graph.
 *
 * Args:
 *   patch: partial EQ fields (``enabled``, ``preset``, ``preamp``, ``bass``, ``mid``, ``treble``,
 *          ``surround``, ``bands``).
 *   opts.persist: write localStorage (default true).
 *   opts.silent: skip the ``eq-change`` event (default false).
 */
export function setEq(patch, { persist: save = true, silent = false } = {}) {
  if (patch.bands) {
    eq.bands = patch.bands.map((v) => clamp(Number(v) || 0, -12, 12));
  }
  for (const k of ["preamp", "bass", "mid", "treble"]) {
    if (patch[k] != null) eq[k] = clamp(Number(patch[k]) || 0, -12, 12);
  }
  if (patch.surround != null) eq.surround = clamp(Number(patch.surround) || 0, 0, 1);
  if (patch.enabled != null) eq.enabled = !!patch.enabled;
  if (patch.preset != null) eq.preset = String(patch.preset);
  applyEqToGraph();
  if (save) persist();
  if (!silent) window.dispatchEvent(new CustomEvent("eq-change", { detail: getEq() }));
  return getEq();
}

/** Apply a named preset (or ``flat``). */
export function applyPreset(name) {
  const p = EQ_PRESETS[name] || EQ_PRESETS.flat;
  return setEq({
    preset: EQ_PRESETS[name] ? name : "flat",
    enabled: true,
    preamp: p.preamp, bass: p.bass, mid: p.mid, treble: p.treble,
    surround: p.surround, bands: [...p.bands],
  });
}

/**
 * Ensure the Web Audio graph exists and the current sound element is wired into it.
 * Safe to call often; must run from a user gesture the first time (AudioContext resume).
 */
export async function ensureGraph() {
  const el = playerApi.soundElement?.();
  if (!el) return false;
  try {
    G.ctx ||= new (window.AudioContext || window.webkitAudioContext)();
    if (G.ctx.state !== "running") await G.ctx.resume();
    if (G.ctx.state !== "running") return false;
    buildGraph(G.ctx);
    if (!G.sources.has(el)) {
      const src = G.ctx.createMediaElementSource(el);
      src.connect(G.input);
      G.sources.set(el, src);
    }
    applyEqToGraph();
    return true;
  } catch {
    return false;
  }
}

/** @deprecated Use ``ensureGraph`` — kept for stage / ambient call sites. */
export async function hookAnalyser() { return ensureGraph(); }

document.addEventListener("visibilitychange", () => {
  if (G.ctx && G.ctx.state !== "running" && !document.hidden) G.ctx.resume().catch(() => {});
});

// Keep the graph alive whenever playback starts (EQ works without opening the stage).
window.addEventListener("player-change", (e) => {
  if (e.detail?.playing) ensureGraph();
});

/**
 * Bass / mid / high levels in 0..1, plus the raw spectrum when the analyser is live.
 */
export function levels(time) {
  const playing = !!playerApi.state?.().playing;
  const el = playerApi.soundElement?.();
  if (G.analyser && el && G.sources.has(el) && playing && !reduceMotion.matches) {
    G.analyser.getByteFrequencyData(G.data);
    const d = G.data, n = d.length;
    const avg = (a, b) => {
      let s = 0;
      for (let i = a; i < b; i++) s += d[i];
      return s / ((b - a) * 255);
    };
    return { bass: avg(1, 8), mid: avg(8, 40), high: avg(40, n), spec: d, real: true };
  }
  const k = playing ? 1 : 0.15;
  const beat = (Math.sin(time / 260) * 0.5 + 0.5) ** 3;
  return {
    bass: (0.25 + beat * 0.45) * k,
    mid: (0.3 + Math.sin(time / 700) * 0.15) * k,
    high: 0.2 * k,
    spec: null,
    real: false,
  };
}

export const prefersReducedMotion = () => reduceMotion.matches;

loadEq();
