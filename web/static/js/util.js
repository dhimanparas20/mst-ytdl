// Small, dependency-free helpers shared by every module.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
/** Escape text for safe insertion into innerHTML / attributes. */
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

/** Build a DOM element from an HTML string (single root). */
export function htmlToEl(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

export function fmtBytes(n) {
  if (n == null || isNaN(n)) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  n = Number(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${u[i]}`;
}

/** → {value: "1.2", unit: "MB/s"} so the stat tile can style them separately. */
export function splitSpeed(bps) {
  if (!bps) return { value: "0", unit: "B/s" };
  const s = fmtBytes(bps).split(" ");
  return { value: s[0], unit: s[1] + "/s" };
}
export const fmtSpeed = (bps) => (bps ? `${fmtBytes(bps)}/s` : "—");

export function fmtDuration(sec) {
  if (sec == null || isNaN(sec)) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

export function fmtEta(sec) {
  if (sec == null || isNaN(sec)) return "";
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

export function fmtDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const today = new Date();
  const same = d.toDateString() === today.toDateString();
  return same
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function debounce(fn, ms) {
  let t;
  const wrapped = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
}

/** Sorted ints → "1,3,5-7" (the syntax the backend's playlist_items accepts). */
export function compressSelection(nums) {
  const a = [...new Set(nums)].sort((x, y) => x - y);
  const out = [];
  for (let i = 0; i < a.length;) {
    let j = i;
    while (j + 1 < a.length && a[j + 1] === a[j] + 1) j++;
    out.push(j > i ? `${a[i]}-${a[j]}` : `${a[i]}`);
    i = j + 1;
  }
  return out.join(",");
}

/** "1,3,5-7" → Set(1,3,5,6,7) clipped to 1..total. Returns null for bad syntax. */
export function parseSelection(spec, total) {
  const picked = new Set();
  for (const part of String(spec || "").replace(/\s+/g, "").split(",")) {
    if (!part) continue;
    const m = part.match(/^(\d*)-(\d*)$/) || part.match(/^(\d+)$/);
    if (!m) return null;
    if (m.length === 2) { picked.add(+m[1]); continue; }
    const lo = m[1] ? +m[1] : 1, hi = m[2] ? +m[2] : total;
    if (lo > hi) return null;
    for (let i = lo; i <= hi && i <= total; i++) picked.add(i);
  }
  return new Set([...picked].filter((i) => i >= 1 && i <= total));
}

const YT_HOST = /^(?:https?:\/\/)?(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)\b/i;
/** Pull YouTube-looking URLs out of free text (one per whitespace-separated token). */
export function extractUrls(text) {
  return String(text || "").split(/\s+/).map((t) => t.trim()).filter((t) => t && YT_HOST.test(t));
}

/** Count-up animation for numbers in stat tiles. */
export function tween(el, to, { duration = 450, format = (v) => Math.round(v) } = {}) {
  const from = Number(el.dataset.v ?? 0);
  if (from === to) { el.textContent = format(to); el.dataset.v = to; return; }
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { el.textContent = format(to); el.dataset.v = to; return; }
  const t0 = performance.now();
  cancelAnimationFrame(el._raf);
  const step = (t) => {
    const k = Math.min(1, (t - t0) / duration);
    const e = 1 - Math.pow(1 - k, 3);
    el.textContent = format(from + (to - from) * e);
    if (k < 1) el._raf = requestAnimationFrame(step); else el.dataset.v = to;
  };
  el._raf = requestAnimationFrame(step);
  el.dataset.v = to;
}

/** Set textContent only if it changed (avoids needless layout work at 1 Hz updates). */
export function setText(el, v) { if (el && el.textContent !== v) el.textContent = v; }
export function setAttr(el, k, v) { if (el && el.getAttribute(k) !== v) el.setAttribute(k, v); }

export function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) || "null");
    localStorage.setItem(key, JSON.stringify(value));
  } catch { /* storage unavailable (private mode) — preferences just won't persist */ }
  return null;
}
