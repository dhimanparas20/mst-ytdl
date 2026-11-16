// Player equalizer panel: enable, presets, bass / mid / treble, 10-band graphic EQ, surround.

import {
  EQ_BANDS, EQ_PRESETS, applyPreset, ensureGraph, getEq, setEq,
} from "./audiofx.js";
import { playerApi } from "./player.js";
import { $ } from "./util.js";

let open = false;

function bandLabel(hz) {
  if (hz >= 1000) return `${hz / 1000}k`;
  return String(hz);
}

function paint() {
  const e = getEq();
  const on = !!e.enabled;
  $("#eq-enable").checked = on;
  $("#eq-panel")?.classList.toggle("eq-on", on);
  $("#mp-eq-btn")?.classList.toggle("on", on);
  $("#st-eq-btn")?.classList.toggle("on", on);
  $("#eq-preamp").value = e.preamp;
  $("#eq-preamp-val").textContent = `${e.preamp > 0 ? "+" : ""}${e.preamp.toFixed(0)} dB`;
  $("#eq-bass").value = e.bass;
  $("#eq-bass-val").textContent = `${e.bass > 0 ? "+" : ""}${e.bass.toFixed(0)}`;
  $("#eq-mid").value = e.mid;
  $("#eq-mid-val").textContent = `${e.mid > 0 ? "+" : ""}${e.mid.toFixed(0)}`;
  $("#eq-treble").value = e.treble;
  $("#eq-treble-val").textContent = `${e.treble > 0 ? "+" : ""}${e.treble.toFixed(0)}`;
  $("#eq-surround").value = Math.round(e.surround * 100);
  $("#eq-surround-val").textContent = `${Math.round(e.surround * 100)}%`;
  EQ_BANDS.forEach((hz, i) => {
    const el = $(`#eq-band-${hz}`);
    if (el) el.value = e.bands[i];
    const lab = $(`#eq-band-${hz}-val`);
    if (lab) lab.textContent = `${e.bands[i] > 0 ? "+" : ""}${e.bands[i].toFixed(0)}`;
  });
  $$preset().forEach((b) => {
    const isCustom = b.dataset.eqPreset === "custom";
    if (isCustom) b.hidden = e.preset !== "custom";
    b.classList.toggle("active", b.dataset.eqPreset === e.preset);
  });
}

function $$preset() {
  return [...document.querySelectorAll("[data-eq-preset]")];
}

function setOpen(v) {
  open = v;
  const panel = $("#eq-panel");
  if (!panel) return;
  panel.hidden = !v;
  $("#mp-eq-btn")?.setAttribute("aria-expanded", String(v));
  $("#st-eq-btn")?.setAttribute("aria-expanded", String(v));
  if (v) {
    paint();
    ensureGraph();  // user gesture — start the AudioContext if needed
  }
}

export function toggleEqualizer() {
  if (!open) {
    // Close queue if it was open (keep player state in sync).
    try { playerApi.setQueueOpen?.(false); } catch { /* player may not be ready */ }
    const q = $("#mp-queue");
    if (q) q.hidden = true;
  }
  setOpen(!open);
}

export function closeEqualizer() {
  setOpen(false);
}

function onSlider(key, el, { scale = 1 } = {}) {
  const v = Number(el.value) * scale;
  setEq({ [key]: v, preset: "custom" });
  paint();
}

export function initEqualizer() {
  const panel = $("#eq-panel");
  if (!panel) return;

  // Build band sliders once
  const host = $("#eq-bands");
  if (host && !host.dataset.ready) {
    host.dataset.ready = "1";
    host.innerHTML = EQ_BANDS.map((hz) => `
      <div class="eq-band">
        <input type="range" id="eq-band-${hz}" class="eq-vrange" min="-12" max="12" step="1" value="0"
          orient="vertical" aria-label="${bandLabel(hz)} Hz" data-eq-band="${hz}">
        <span class="eq-band-val" id="eq-band-${hz}-val">0</span>
        <span class="eq-band-hz">${bandLabel(hz)}</span>
      </div>`).join("");
  }

  const presets = $("#eq-presets");
  if (presets && !presets.dataset.ready) {
    presets.dataset.ready = "1";
    presets.innerHTML = Object.entries(EQ_PRESETS).map(([id, p]) =>
      `<button type="button" class="eq-preset" data-eq-preset="${id}">${p.label}</button>`
    ).join("") + `<button type="button" class="eq-preset" data-eq-preset="custom" hidden>Custom</button>`;
  }

  $("#mp-eq-btn")?.addEventListener("click", () => toggleEqualizer());
  $("#st-eq-btn")?.addEventListener("click", () => toggleEqualizer());
  $("#eq-close")?.addEventListener("click", () => closeEqualizer());
  $("#eq-enable")?.addEventListener("change", (e) => {
    setEq({ enabled: e.target.checked });
    ensureGraph();
    paint();
  });
  $("#eq-reset")?.addEventListener("click", () => {
    applyPreset("flat");
    setEq({ enabled: $("#eq-enable").checked, preset: "flat" });
    paint();
  });

  presets?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-eq-preset]");
    if (!b || b.dataset.eqPreset === "custom") return;
    applyPreset(b.dataset.eqPreset);
    paint();
    ensureGraph();
  });

  $("#eq-preamp")?.addEventListener("input", (e) => onSlider("preamp", e.target));
  $("#eq-bass")?.addEventListener("input", (e) => onSlider("bass", e.target));
  $("#eq-mid")?.addEventListener("input", (e) => onSlider("mid", e.target));
  $("#eq-treble")?.addEventListener("input", (e) => onSlider("treble", e.target));
  $("#eq-surround")?.addEventListener("input", (e) => onSlider("surround", e.target, { scale: 0.01 }));

  host?.addEventListener("input", (e) => {
    const el = e.target.closest("[data-eq-band]");
    if (!el) return;
    const hz = Number(el.dataset.eqBand);
    const idx = EQ_BANDS.indexOf(hz);
    if (idx < 0) return;
    const bands = [...getEq().bands];
    bands[idx] = Number(el.value);
    setEq({ bands, preset: "custom" });
    paint();
  });

  window.addEventListener("eq-change", paint);
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && open) { e.preventDefault(); closeEqualizer(); }
  });

  paint();
}
