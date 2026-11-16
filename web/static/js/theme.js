// Accent theme: presets + circular HSV wheel (More) for any shade.
// Stored in localStorage as ``mst.theme`` = { hex: "#6a58d6" }. Reset clears it.

import { $, store } from "./util.js";

const KEY = "mst.theme";
export const DEFAULT_HEX = "#6a58d6";

const PRESETS = [
  "#6a58d6", "#ef4444", "#f97316", "#eab308", "#22c55e", "#14b8a6",
  "#06b6d4", "#3b82f6", "#ec4899", "#a855f7", "#f43f5e", "#84cc16",
];

function clamp(n, a, b) { return Math.min(b, Math.max(a, n)); }

function hexToRgb(hex) {
  const h = String(hex || "").replace("#", "").trim();
  if (!/^[0-9a-fA-F]{6}$/.test(h)) return null;
  return { r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16), b: parseInt(h.slice(4, 6), 16) };
}

function rgbToHex({ r, g, b }) {
  const p = (n) => clamp(Math.round(n), 0, 255).toString(16).padStart(2, "0");
  return `#${p(r)}${p(g)}${p(b)}`;
}

function rgbToHsl({ r, g, b }) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h = 0, s = 0;
  const l = (max + min) / 2;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    switch (max) {
      case r: h = ((g - b) / d + (g < b ? 6 : 0)); break;
      case g: h = (b - r) / d + 2; break;
      default: h = (r - g) / d + 4; break;
    }
    h /= 6;
  }
  return { h: h * 360, s, l };
}

function hslToRgb(h, s, l) {
  h = ((h % 360) + 360) % 360 / 360;
  let r, g, b;
  if (s === 0) { r = g = b = l; }
  else {
    const hue2rgb = (p, q, t) => {
      if (t < 0) t += 1; if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    };
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    r = hue2rgb(p, q, h + 1 / 3);
    g = hue2rgb(p, q, h);
    b = hue2rgb(p, q, h - 1 / 3);
  }
  return { r: r * 255, g: g * 255, b: b * 255 };
}

/** Build light / mid / deep accent steps from any hex (mid = the colour the user picked). */
export function paletteFrom(hex) {
  const rgb = hexToRgb(hex) || hexToRgb(DEFAULT_HEX);
  const { h, s, l } = rgbToHsl(rgb);
  const sat = clamp(s, 0.35, 0.9);
  // Keep the picked lightness — the brightness slider must change the theme.
  const midL = clamp(l, 0.18, 0.78);
  const g2 = { r: rgb.r, g: rgb.g, b: rgb.b };  // exact pick
  const g1 = hslToRgb(h, sat, clamp(midL + 0.16, 0.32, 0.88));
  const g3 = hslToRgb(h, Math.min(sat, 0.85), clamp(midL - 0.18, 0.12, 0.55));
  const soft = hslToRgb(h, Math.min(sat, 0.55), clamp(midL + 0.28, 0.7, 0.94));
  const midSoft = hslToRgb(h, Math.min(sat, 0.5), clamp(midL + 0.2, 0.58, 0.88));
  return {
    hex: rgbToHex(g2),
    g1: rgbToHex(g1), g2: rgbToHex(g2), g3: rgbToHex(g3),
    g1rgb: `${Math.round(g1.r)}, ${Math.round(g1.g)}, ${Math.round(g1.b)}`,
    g2rgb: `${Math.round(g2.r)}, ${Math.round(g2.g)}, ${Math.round(g2.b)}`,
    g3rgb: `${Math.round(g3.r)}, ${Math.round(g3.g)}, ${Math.round(g3.b)}`,
    soft: rgbToHex(soft), midSoft: rgbToHex(midSoft),
  };
}

export function applyTheme(hex) {
  const root = document.documentElement;
  const use = hex && hexToRgb(hex) ? hex : DEFAULT_HEX;
  const p = paletteFrom(use);
  const set = (k, v) => root.style.setProperty(k, v);
  set("--g1", p.g1); set("--g2", p.g2); set("--g3", p.g3);
  set("--g1-rgb", p.g1rgb); set("--g2-rgb", p.g2rgb); set("--g3-rgb", p.g3rgb);
  set("--grad", `linear-gradient(135deg, ${p.g1} 0%, ${p.g2} 55%, ${p.g3} 100%)`);
  set("--grad-text", `linear-gradient(135deg, ${p.soft} 0%, ${p.midSoft} 55%, ${p.g1} 100%)`);
  set("--grad-soft", `linear-gradient(135deg, rgba(${p.g1rgb}, .20), rgba(${p.g2rgb}, .14) 55%, rgba(${p.g3rgb}, .20))`);
  set("--info", p.midSoft);
  set("--glow-a", `rgba(${p.g1rgb}, .10)`);
  set("--glow-b", `rgba(${p.g3rgb}, .13)`);
  set("--glow-c", `rgba(${p.g2rgb}, .10)`);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", p.g3);
  return p;
}

export function currentThemeHex() {
  const saved = store(KEY);
  return (saved && saved.hex) || DEFAULT_HEX;
}

export function saveTheme(hex) {
  if (!hex || hex.toLowerCase() === DEFAULT_HEX) {
    try { localStorage.removeItem(KEY); } catch { /* */ }
    applyTheme(DEFAULT_HEX);
    return DEFAULT_HEX;
  }
  const p = applyTheme(hex);
  store(KEY, { hex: p.hex });
  return p.hex;
}

// ── wheel state (wheelLit is 18–78 percent lightness) ──
let wheelHue = 250;
let wheelSat = 0.65;
let wheelLit = 55;
let wheelDrawn = false;

function wheelHex() {
  return rgbToHex(hslToRgb(wheelHue, wheelSat, wheelLit / 100));
}

function syncWheelFromHex(hex) {
  const rgb = hexToRgb(hex) || hexToRgb(DEFAULT_HEX);
  const hsl = rgbToHsl(rgb);
  wheelHue = hsl.h;
  wheelSat = clamp(hsl.s, 0.05, 1);
  wheelLit = clamp(hsl.l * 100, 18, 78);
  const light = $("#theme-light");
  if (light) light.value = String(Math.round(wheelLit));
}

function paintPicker(hex) {
  const input = $("#theme-color");
  const hexEl = $("#theme-hex");
  const preview = $("#theme-preview");
  const swatches = $("#theme-swatches");
  if (input) input.value = hex;
  if (hexEl) hexEl.textContent = hex;
  if (preview) preview.style.background = hex;
  if (swatches) {
    swatches.querySelectorAll("[data-theme]").forEach((b) => {
      b.classList.toggle("active", b.dataset.theme.toLowerCase() === hex.toLowerCase());
    });
  }
  drawWheelMarker();
}

function onPick(hex, { fromWheel = false } = {}) {
  // Persist the exact hex (incl. brightness). Do not re-sync from the
  // remapped palette while dragging the wheel/slider — that fought the control.
  const next = saveTheme(hex);
  if (!fromWheel) syncWheelFromHex(hex);
  paintPicker(fromWheel ? hex : next);
}

function drawWheelMarker() {
  const canvas = $("#theme-wheel");
  if (!canvas || !wheelDrawn) return;
  // redraw base cheaply: only marker overlay via second pass — re-call full draw is heavy;
  // store base image
  if (!canvas._base) {
    drawWheelBase(canvas);
  }
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(canvas._base, 0, 0);
  const size = canvas.width;
  const cx = size / 2, cy = size / 2;
  const outer = size / 2 - 2;
  const inner = outer * 0.62;
  const ringR = (outer + inner) / 2;
  const rad = (wheelHue - 90) * Math.PI / 180;
  // hue knob on ring
  const hx = cx + Math.cos(rad) * ringR;
  const hy = cy + Math.sin(rad) * ringR;
  ctx.beginPath();
  ctx.arc(hx, hy, 7, 0, Math.PI * 2);
  ctx.fillStyle = "#fff";
  ctx.fill();
  ctx.strokeStyle = "rgba(0,0,0,.45)";
  ctx.lineWidth = 2;
  ctx.stroke();
  // sat knob in disc
  const satR = wheelSat * (inner - 1);
  const sx = cx + Math.cos(rad) * satR;
  const sy = cy + Math.sin(rad) * satR;
  ctx.beginPath();
  ctx.arc(sx, sy, 6, 0, Math.PI * 2);
  ctx.fillStyle = wheelHex();
  ctx.fill();
  ctx.strokeStyle = "#fff";
  ctx.lineWidth = 2;
  ctx.stroke();
}

function drawWheelBase(canvas) {
  const off = document.createElement("canvas");
  off.width = canvas.width;
  off.height = canvas.height;
  const ctx = off.getContext("2d");
  const size = off.width;
  const cx = size / 2, cy = size / 2;
  const outer = size / 2 - 2;
  const inner = outer * 0.62;
  for (let a = 0; a < 360; a++) {
    const rad = (a - 90) * Math.PI / 180;
    ctx.beginPath();
    ctx.strokeStyle = `hsl(${a}, 100%, 50%)`;
    ctx.lineWidth = outer - inner;
    ctx.arc(cx, cy, (outer + inner) / 2, rad, rad + Math.PI / 180 + 0.02);
    ctx.stroke();
  }
  const img = ctx.createImageData(size, size);
  const data = img.data;
  const lit = 0.55;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = x - cx, dy = y - cy;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist >= inner - 0.5) continue;
      const ang = (Math.atan2(dy, dx) * 180 / Math.PI + 90 + 360) % 360;
      const sat = clamp(dist / (inner - 1), 0, 1);
      const { r, g, b } = hslToRgb(ang, sat, lit);
      const i = (y * size + x) * 4;
      data[i] = r; data[i + 1] = g; data[i + 2] = b; data[i + 3] = 255;
    }
  }
  // composite: only write non-transparent from img onto ring canvas — putImageData overwrites ring;
  // so draw disc onto a temp then composite
  const disc = document.createElement("canvas");
  disc.width = size; disc.height = size;
  disc.getContext("2d").putImageData(img, 0, 0);
  ctx.drawImage(disc, 0, 0);
  canvas._base = off;
  wheelDrawn = true;
}

function pickFromPointer(ev, canvas) {
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  const x = (ev.clientX - rect.left) * scaleX;
  const y = (ev.clientY - rect.top) * scaleY;
  const cx = canvas.width / 2, cy = canvas.height / 2;
  const outer = canvas.width / 2 - 2;
  const inner = outer * 0.62;
  const dx = x - cx, dy = y - cy;
  const dist = Math.sqrt(dx * dx + dy * dy);
  const ang = (Math.atan2(dy, dx) * 180 / Math.PI + 90 + 360) % 360;
  if (dist >= inner - 2 && dist <= outer + 2) {
    wheelHue = ang;
  } else if (dist < inner) {
    wheelHue = ang;
    wheelSat = clamp(dist / (inner - 1), 0.05, 1);
  } else {
    return;
  }
  onPick(wheelHex(), { fromWheel: true });
}

function wireWheel() {
  const canvas = $("#theme-wheel");
  const box = $("#theme-wheel-box");
  const more = $("#theme-more");
  if (!canvas || !box || !more) return;

  more.addEventListener("click", () => {
    const open = box.hidden;
    box.hidden = !open;
    more.setAttribute("aria-expanded", open ? "true" : "false");
    more.innerHTML = open
      ? `<i class="bi bi-chevron-up"></i>Hide colour wheel`
      : `<i class="bi bi-palette2"></i>More — pick any shade`;
    if (open) {
      syncWheelFromHex(currentThemeHex());
      drawWheelBase(canvas);
      drawWheelMarker();
    }
  });

  let dragging = false;
  canvas.addEventListener("pointerdown", (e) => {
    dragging = true;
    canvas.setPointerCapture(e.pointerId);
    pickFromPointer(e, canvas);
  });
  canvas.addEventListener("pointermove", (e) => { if (dragging) pickFromPointer(e, canvas); });
  canvas.addEventListener("pointerup", () => { dragging = false; });
  canvas.addEventListener("pointercancel", () => { dragging = false; });

  $("#theme-light")?.addEventListener("input", (e) => {
    wheelLit = clamp(Number(e.target.value) || 55, 18, 78);
    const hex = wheelHex();
    onPick(hex, { fromWheel: true });
  });
}

/** Boot: apply saved theme; wire Settings presets + wheel. */
export function initTheme() {
  const hex = currentThemeHex();
  applyTheme(hex);
  syncWheelFromHex(hex);

  const swatches = $("#theme-swatches");
  if (swatches && !swatches.childElementCount) {
    swatches.innerHTML = PRESETS.map((c) =>
      `<button type="button" class="theme-swatch" data-theme="${c}" style="--sw:${c}" aria-label="Theme ${c}" title="${c}"></button>`
    ).join("");
  }
  paintPicker(hex);

  $("#theme-color")?.addEventListener("input", (e) => onPick(e.target.value));
  $("#theme-color")?.addEventListener("change", (e) => onPick(e.target.value));
  swatches?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-theme]");
    if (b) onPick(b.dataset.theme);
  });
  $("#theme-reset")?.addEventListener("click", () => onPick(DEFAULT_HEX));
  wireWheel();
}
