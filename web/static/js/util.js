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

/** yt-dlp / account messages that mean the user must (re)link YouTube cookies. */
const COOKIE_AUTH_RE = /cookies? are no longer valid|sign in to confirm|confirm you.?re not a bot|login cookie|missing required authentication|http error 401|cookies may have expired|add your youtube cookies|no longer valid|rotated in the browser/i;

export function isCookieAuthError(msg) {
  return !!msg && COOKIE_AUTH_RE.test(String(msg));
}

/** Show the global cookie health banner (dismissible; CTA opens the cookies modal). */
export function showCookieBanner(reason) {
  const el = $("#cookie-banner");
  if (!el) return;
  const text = $("#cookie-banner-text");
  if (text) text.textContent = reason || "Your YouTube cookies look invalid or missing — link them to fix Mine, search and downloads.";
  el.hidden = false;
}

export function hideCookieBanner() {
  const el = $("#cookie-banner");
  if (el) el.hidden = true;
}

/** If ``msg`` looks like a cookie/auth failure, show the banner. */
export function maybeCookieBanner(msg) {
  if (isCookieAuthError(msg)) showCookieBanner(String(msg).slice(0, 220));
}

/**
 * Topbar cookie chip state from a ``CookieStatus``.
 * @returns {{text: string, tone: "ok"|"warn"|"none", title: string} | null} null = status unavailable (hide the chip).
 */
export function cookieChipState(c, now = Date.now()) {
  if (!c) return null;
  if (!c.loaded) return { text: "Cookies · none", tone: "none", title: "No YouTube cookies loaded — click to add some" };
  if (c.source === "browser" && !c.count) return { text: "Cookies · browser", tone: "ok", title: `Read from the ${c.browser || "browser"} profile` };
  if (!c.login) return { text: "Cookies · not signed in", tone: "warn", title: "Cookies are loaded but carry no login session — click to fix" };
  const warn = (c.warnings || [])[0];
  const ms = c.expires_at ? Date.parse(c.expires_at) : NaN;
  if (Number.isNaN(ms)) return { text: "Cookies · signed in", tone: warn ? "warn" : "ok", title: warn || "Signed in" };
  if (ms <= now) return { text: "Cookies · expired", tone: "warn", title: "The login cookies have expired — click to refresh them" };
  const days = Math.floor((ms - now) / 86400000);
  const when = new Date(ms).toLocaleDateString();
  if (days > 14) return { text: "Cookies · signed in", tone: warn ? "warn" : "ok", title: warn || `Signed in — login expires ${when}` };
  return {
    text: `Cookies · signed in · ${days < 1 ? "<1 d" : `${days} d`} left`,
    tone: days <= 3 || warn ? "warn" : "ok",
    title: warn || `Signed in — login expires ${when}`,
  };
}

/** Compact 1…N pager (max 10). Returns "" when only one page. */
export function pagerHTML({ page = 1, pages = 1 } = {}) {
  const p = Math.max(1, Number(page) || 1);
  const n = Math.max(1, Math.min(10, Number(pages) || 1));
  if (n <= 1) return "";
  const btns = [];
  for (let i = 1; i <= n; i++) {
    btns.push(`<button type="button" class="pager-num${i === p ? " active" : ""}" data-page="${i}">${i}</button>`);
  }
  return `<nav class="results-pager" aria-label="Result pages">
    <button type="button" class="pager-nav" data-page="${p - 1}" ${p <= 1 ? "disabled" : ""} aria-label="Previous page"><i class="bi bi-chevron-left"></i></button>
    <div class="pager-nums">${btns.join("")}</div>
    <button type="button" class="pager-nav" data-page="${p + 1}" ${p >= n ? "disabled" : ""} aria-label="Next page"><i class="bi bi-chevron-right"></i></button>
  </nav>`;
}

