// The floating video window: drag by its header, resize from any corner (always 16:9),
// snap to screen edges, S/M/L presets, fullscreen and picture-in-picture. Position and size
// are remembered. On phones it docks above the player instead (CSS) and is not draggable.

import { clampWindow, resizeFromCorner, snapWindow } from "./queue-logic.js";
import { $, store } from "./util.js";

const KEY = "mst.vwin";
const MIN_W = 260;
const HEAD_H = 38;
const PRESETS = [320, 480, 720];
const mobile = matchMedia("(max-width: 767.98px)");

let el;
let geo = { x: null, y: null, w: 440 };

const heightOf = (w) => Math.round((w * 9) / 16) + HEAD_H;
const isMobile = () => mobile.matches;

function defaults() {
  const w = Math.min(geo.w || 440, innerWidth - 24);
  return { x: innerWidth - w - 18, y: Math.max(12, innerHeight - heightOf(w) - 168), w };
}

function apply() {
  if (!el || isMobile()) {  // phones: CSS positions it
    ["left", "top", "width"].forEach((p) => el?.style.removeProperty(p));
    return;
  }
  if (geo.x == null) Object.assign(geo, defaults());
  const maxW = Math.min(innerWidth - 16, (innerHeight - 16 - HEAD_H) * (16 / 9));
  geo.w = Math.min(Math.max(geo.w, MIN_W), Math.max(MIN_W, maxW));
  const c = clampWindow({ x: geo.x, y: geo.y, w: geo.w, h: heightOf(geo.w) }, innerWidth, innerHeight);
  Object.assign(geo, c);
  el.style.left = `${geo.x}px`;
  el.style.top = `${geo.y}px`;
  el.style.width = `${geo.w}px`;
}

const save = () => store(KEY, { x: geo.x, y: geo.y, w: geo.w, open: !el.hidden });

export const isVisible = () => !!el && !el.hidden;
export const isFullscreen = () => document.fullscreenElement === el;

export function showWindow() {
  if (!el.hidden) return;
  el.hidden = false;
  document.body.classList.add("has-window");
  apply();
  save();
}

export function hideWindow() {
  if (isFullscreen()) document.exitFullscreen?.();
  el.hidden = true;
  document.body.classList.remove("has-window");
  save();
}

export function toggleWindow() { isVisible() ? hideWindow() : showWindow(); }

export function toggleFullscreen() {
  if (isFullscreen()) document.exitFullscreen?.();
  else el.requestFullscreen?.().catch(() => {});
}

/** Cycle small → medium → large, keeping the window on screen. */
export function cycleSize() {
  const i = PRESETS.findIndex((w) => Math.abs(w - geo.w) < 24);
  const nextW = PRESETS[(i + 1) % PRESETS.length];
  const right = geo.x + geo.w, bottom = geo.y + heightOf(geo.w);
  geo.w = nextW;                       // grow/shrink toward the top-left, like a bottom-right anchored panel
  geo.x = right - nextW; geo.y = bottom - heightOf(nextW);
  el.classList.add("resizing"); apply(); save();
  setTimeout(() => el.classList.remove("resizing"), 50);
}

function startDrag(e) {
  if (isMobile() || isFullscreen() || e.button > 0 || e.target.closest("button")) return;
  const head = $("#vwin-head");
  head.setPointerCapture(e.pointerId);
  const r = el.getBoundingClientRect();
  const dx = e.clientX - r.left, dy = e.clientY - r.top;
  el.classList.add("dragging");
  const move = (ev) => { geo.x = ev.clientX - dx; geo.y = ev.clientY - dy; apply(); };
  const up = () => {
    head.removeEventListener("pointermove", move);
    el.classList.remove("dragging");
    Object.assign(geo, snapWindow({ x: geo.x, y: geo.y, w: geo.w, h: heightOf(geo.w) }, innerWidth, innerHeight));
    apply(); save();
  };
  head.addEventListener("pointermove", move);
  head.addEventListener("pointerup", up, { once: true });
  head.addEventListener("pointercancel", up, { once: true });
}

function startResize(e) {
  if (isMobile() || isFullscreen()) return;
  e.preventDefault(); e.stopPropagation();
  const grip = e.currentTarget;
  const corner = grip.dataset.grip;            // tl | tr | bl | br
  grip.setPointerCapture(e.pointerId);
  const start = { x: geo.x, y: geo.y, w: geo.w, right: geo.x + geo.w, bottom: geo.y + heightOf(geo.w), px: e.clientX };
  const maxW = Math.min(innerWidth - 16, (innerHeight - 16 - HEAD_H) * (16 / 9));
  el.classList.add("resizing");
  const move = (ev) => {
    const w = resizeFromCorner(start, corner, ev.clientX - start.px, MIN_W, maxW);
    geo.w = w;
    if (corner.endsWith("l")) geo.x = start.right - w;       // left grips keep the right edge fixed
    if (corner.startsWith("t")) geo.y = start.bottom - heightOf(w);  // top grips keep the bottom edge fixed
    apply();
  };
  const up = () => { grip.removeEventListener("pointermove", move); el.classList.remove("resizing"); save(); };
  grip.addEventListener("pointermove", move);
  grip.addEventListener("pointerup", up, { once: true });
  grip.addEventListener("pointercancel", up, { once: true });
}

/** Wire the window. ``onPlayToggle`` is called for a click on the picture (play/pause). */
export function initWindow({ onPlayToggle } = {}) {
  el = $("#vwin");
  Object.assign(geo, store(KEY) || {});
  apply();

  $("#vwin-head").addEventListener("pointerdown", startDrag);
  $("#vwin-head").addEventListener("dblclick", (e) => { if (!e.target.closest("button")) toggleFullscreen(); });
  el.querySelectorAll(".grip").forEach((g) => g.addEventListener("pointerdown", startResize));
  $("#vw-close").addEventListener("click", hideWindow);
  $("#vw-fs").addEventListener("click", toggleFullscreen);
  $("#vw-size").addEventListener("click", cycleSize);
  $("#vw-pip").addEventListener("click", async () => {
    const v = $("#mp-v");
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else await v.requestPictureInPicture();
    } catch { /* not allowed right now (no video yet) */ }
  });
  if (!document.pictureInPictureEnabled) $("#vw-pip").hidden = true;

  // click the picture = play/pause, double-click = fullscreen (like every video site)
  const body = $("#vwin-body");
  let clickTimer = null;
  body.addEventListener("click", (e) => {
    if (e.target.closest(".vwin-ctl, .vwin-msg a")) return;
    if (e.detail > 1) { clearTimeout(clickTimer); toggleFullscreen(); return; }
    clickTimer = setTimeout(() => onPlayToggle?.(), 220);
  });

  // controls fade away while playing and the mouse is still
  let idleTimer = null;
  const wake = () => {
    el.classList.remove("idle");
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (!el.querySelector(".dropdown-menu.show")) el.classList.add("idle"); }, 2600);
  };
  body.addEventListener("pointermove", wake);
  body.addEventListener("pointerdown", wake);
  el.addEventListener("pointerleave", () => { idleTimer = setTimeout(() => el.classList.add("idle"), 900); });
  window.addEventListener("keydown", wake);

  document.addEventListener("fullscreenchange", () => {
    el.classList.toggle("is-fs", isFullscreen());
    $("#vw-fs i").className = `bi bi-${isFullscreen() ? "fullscreen-exit" : "arrows-fullscreen"}`;
    wake();
  });
  window.addEventListener("resize", () => { apply(); });
  mobile.addEventListener("change", apply);
}
