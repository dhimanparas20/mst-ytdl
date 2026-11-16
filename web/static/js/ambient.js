// Page-wide music ambient: a soft reactive canvas behind the UI while the mini player is
// playing. Off by default (Settings → Look). Shares the Web Audio analyser with stage.js via
// audiofx.js — never wires the media element a second time. Hidden while immersive stage is open.

import { hookAnalyser, levels, prefersReducedMotion } from "./audiofx.js";
import { playerApi } from "./player.js";
import { getUiPrefs } from "./prefs.js";
import { $ } from "./util.js";

const S = { raf: 0, live: false, particles: [] };
let cv, g;

function enabled() {
  return !!getUiPrefs().ambientViz
    && !getUiPrefs().reduceMotion
    && !prefersReducedMotion();
}

function shouldRun() {
  return enabled()
    && !!playerApi.current?.()
    && !!playerApi.state?.().playing
    && !document.body.classList.contains("stage-open");
}

function themeRgb(name, fallback) {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!raw) return fallback;
  const parts = raw.split(/[,\s]+/).map(Number).filter((n) => Number.isFinite(n));
  return parts.length >= 3 ? parts.slice(0, 3) : fallback;
}

function fit() {
  const dpr = Math.min(devicePixelRatio || 1, 2);
  const w = innerWidth, h = innerHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [w, h];
}

function ensureParticles(n) {
  while (S.particles.length < n) {
    S.particles.push({
      x: Math.random(), y: Math.random(),
      vx: (Math.random() - 0.5) * 0.00035,
      vy: (Math.random() - 0.5) * 0.00035,
      r: 0.6 + Math.random() * 1.8,
      a: 0.15 + Math.random() * 0.35,
    });
  }
  if (S.particles.length > n) S.particles.length = n;
}

function draw(time, lv) {
  const [w, h] = fit();
  g.clearRect(0, 0, w, h);
  const c1 = themeRgb("--g1-rgb", [140, 123, 240]);
  const c2 = themeRgb("--g2-rgb", [106, 88, 214]);
  const c3 = themeRgb("--g3-rgb", [74, 58, 168]);
  const beat = lv.bass;
  const mid = lv.mid;
  const high = lv.high;

  // soft vignette wash so content stays readable
  const wash = g.createRadialGradient(w * 0.5, h * 0.35, 0, w * 0.5, h * 0.5, Math.max(w, h) * 0.72);
  wash.addColorStop(0, `rgba(${c1[0]},${c1[1]},${c1[2]},${0.10 + beat * 0.16})`);
  wash.addColorStop(0.55, `rgba(${c2[0]},${c2[1]},${c2[2]},${0.06 + mid * 0.10})`);
  wash.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = wash;
  g.fillRect(0, 0, w, h);

  // drifting aurora blobs
  const blobs = [
    { x: 0.18 + Math.sin(time / 4200) * 0.04, y: 0.28 + Math.cos(time / 5100) * 0.05, r: 0.34 + beat * 0.12, c: c1 },
    { x: 0.78 + Math.cos(time / 4800) * 0.05, y: 0.22 + Math.sin(time / 3900) * 0.04, r: 0.30 + mid * 0.10, c: c2 },
    { x: 0.52 + Math.sin(time / 5600) * 0.06, y: 0.78 + Math.cos(time / 4500) * 0.04, r: 0.38 + beat * 0.08, c: c3 },
  ];
  g.globalCompositeOperation = "lighter";
  for (const b of blobs) {
    const R = Math.max(w, h) * b.r;
    const grd = g.createRadialGradient(b.x * w, b.y * h, 0, b.x * w, b.y * h, R);
    grd.addColorStop(0, `rgba(${b.c[0]},${b.c[1]},${b.c[2]},${0.22 + beat * 0.28})`);
    grd.addColorStop(0.45, `rgba(${b.c[0]},${b.c[1]},${b.c[2]},${0.08 + mid * 0.12})`);
    grd.addColorStop(1, `rgba(${b.c[0]},${b.c[1]},${b.c[2]},0)`);
    g.fillStyle = grd;
    g.beginPath();
    g.arc(b.x * w, b.y * h, R, 0, Math.PI * 2);
    g.fill();
  }

  // edge spectrum ribbons (bottom + faint top)
  const bars = 48;
  const bw = w / bars;
  g.globalAlpha = 0.55 + high * 0.25;
  for (let i = 0; i < bars; i++) {
    let v;
    if (lv.spec) {
      const idx = Math.floor((i / bars) * (lv.spec.length * 0.72));
      v = lv.spec[idx] / 255;
    } else {
      v = (0.25 + 0.55 * Math.abs(Math.sin(time / 380 + i * 0.22))) * (0.35 + beat);
    }
    const bh = (18 + v * (56 + beat * 40)) * (0.55 + mid * 0.6);
    const x = i * bw + bw * 0.15;
    const col = i % 3 === 0 ? c1 : i % 3 === 1 ? c2 : c3;
    g.fillStyle = `rgba(${col[0]},${col[1]},${col[2]},${0.18 + v * 0.45})`;
    g.fillRect(x, h - bh - 4, bw * 0.7, bh);
    g.fillRect(x, 4, bw * 0.7, bh * 0.28);
  }
  g.globalAlpha = 1;

  // spark particles on highs
  ensureParticles(36);
  g.globalCompositeOperation = "lighter";
  for (const p of S.particles) {
    p.x += p.vx * (1 + high * 4);
    p.y += p.vy * (1 + mid * 3) - beat * 0.00015;
    if (p.x < 0 || p.x > 1) p.vx *= -1;
    if (p.y < 0 || p.y > 1) p.vy *= -1;
    p.x = Math.min(1, Math.max(0, p.x));
    p.y = Math.min(1, Math.max(0, p.y));
    const a = p.a * (0.35 + high * 1.4 + beat * 0.5);
    g.fillStyle = `rgba(${c1[0]},${c1[1]},${c1[2]},${a})`;
    g.beginPath();
    g.arc(p.x * w, p.y * h, p.r * (1 + beat), 0, Math.PI * 2);
    g.fill();
  }
  g.globalCompositeOperation = "source-over";
}

function stop({ clear = false } = {}) {
  cancelAnimationFrame(S.raf);
  S.raf = 0;
  S.live = false;
  document.body.classList.remove("ambient-live");
  if (cv) {
    cv.hidden = true;
    if (clear && g) {
      const [w, h] = fit();
      g.clearRect(0, 0, w, h);
    }
  }
}

function frame(time) {
  if (!shouldRun()) {
    stop({ clear: true });
    return;
  }
  draw(time, levels(time));
  S.raf = requestAnimationFrame(frame);
}

function start() {
  if (!cv || !shouldRun()) {
    if (!enabled() || !playerApi.state?.().playing) stop({ clear: true });
    return;
  }
  cv.hidden = false;
  document.body.classList.add("ambient-live");
  if (S.live) return;
  S.live = true;
  hookAnalyser();
  cancelAnimationFrame(S.raf);
  S.raf = requestAnimationFrame(frame);
}

/** Re-evaluate whether the ambient canvas should run (pref / play / stage). */
export function syncAmbient() {
  if (shouldRun()) start();
  else stop({ clear: !enabled() || document.body.classList.contains("stage-open") });
}

export function initAmbient() {
  cv = $("#ambient-fx");
  if (!cv) {
    cv = document.createElement("canvas");
    cv.id = "ambient-fx";
    cv.className = "ambient-fx";
    cv.setAttribute("aria-hidden", "true");
    cv.hidden = true;
    document.body.prepend(cv);
  }
  g = cv.getContext("2d");

  window.addEventListener("player-change", (e) => {
    if (e.detail?.playing) {
      hookAnalyser();
      start();
    } else {
      // pause → freeze & fade out (CSS), then clear once the transition ends
      document.body.classList.remove("ambient-live");
      cancelAnimationFrame(S.raf);
      S.raf = 0;
      S.live = false;
      setTimeout(() => { if (!shouldRun()) stop({ clear: true }); }, 480);
    }
  });
  window.addEventListener("player-track", () => { if (shouldRun()) { hookAnalyser(); start(); } });
  window.addEventListener("ui-prefs", syncAmbient);
  window.addEventListener("stage-change", syncAmbient);
  window.addEventListener("resize", () => { if (S.live) fit(); });

  syncAmbient();
}
