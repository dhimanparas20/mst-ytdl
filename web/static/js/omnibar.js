// Universal top-bar search: paste links OR type a query.
// Results paint in the main Explore feed (feedSearch) — not a dropdown card.
// Empty-focus panel still shows recent / popular chips only.

import { api } from "./api.js";
import { acceptLinks } from "./composer.js";
import { feedSearch } from "./explore.js";
import { getUiPrefs } from "./prefs.js";
import { toast } from "./ui.js";
import { $, debounce, esc, extractUrls, store } from "./util.js";

const HIST_KEY = "mst.searchHistory";
const HIST_MAX = 12;

let open = false;
let lastQ = "";
let lastMusic = null;  // last Music-pref we searched with (ui-prefs re-run only on flip)
let popular = [];
let mode = "idle"; // idle | links

const panel = () => $("#omnibar-panel");
const input = () => $("#omnibar-input");
const host = () => $("#omnibar-results");
const home = () => $("#omnibar-home");

function historyList() {
  const v = store(HIST_KEY);
  return Array.isArray(v) ? v.filter((x) => typeof x === "string" && x.trim()) : [];
}

function pushHistory(q) {
  const clean = q.trim().toLowerCase();
  if (clean.length < 2) return;
  const next = [clean, ...historyList().filter((x) => x !== clean)].slice(0, HIST_MAX);
  store(HIST_KEY, next);
}

function setOpen(v) {
  open = v;
  document.body.classList.toggle("omnibar-open", v);
  const p = panel();
  if (!p) return;
  if (v) {
    p.hidden = false;
    p.classList.remove("is-leaving");
    requestAnimationFrame(() => { if (open) p.classList.add("is-open"); });
  } else {
    p.classList.remove("is-open");
    p.classList.add("is-leaving");
    setTimeout(() => { if (!open) { p.hidden = true; p.classList.remove("is-leaving"); } }, 180);
  }
}

function paintStatus(html) {
  const el = $("#omnibar-status");
  if (el) el.innerHTML = html || "";
}

function chipRow(title, icon, items, attr) {
  if (!items.length) return "";
  return `<div class="omni-sec">
    <div class="omni-sec-h"><i class="bi bi-${icon}"></i>${esc(title)}</div>
    <div class="omni-chips">${items.map((q) =>
      `<button type="button" class="omni-chip" ${attr}="${esc(q)}">${esc(q)}</button>`
    ).join("")}</div>
  </div>`;
}

function showHome(filter = "") {
  mode = "idle";
  const f = filter.trim().toLowerCase();
  let hist = historyList();
  if (f) hist = hist.filter((h) => h.includes(f));
  let pops = popular;
  if (f) pops = pops.filter((h) => h.includes(f));
  const histSet = new Set(hist);
  pops = pops.filter((p) => !histSet.has(p));
  const fallback = !hist.length && !pops.length
    ? ["lofi hip hop", "bollywood hits", "punjabi songs", "arijit singh", "workout mix"]
      .filter((x) => !f || x.includes(f))
    : [];
  home().hidden = false;
  host().innerHTML = "";
  home().innerHTML = [
    chipRow("Recent", "clock-history", hist.slice(0, 8), "data-hist"),
    chipRow("Popular on this server", "fire", pops.slice(0, 8), "data-sug"),
    chipRow("Try searching", "stars", fallback.slice(0, 5), "data-sug"),
    hist.length
      ? `<button type="button" class="btn-ghost sm omni-clear-hist" data-clear-hist><i class="bi bi-trash3"></i>Clear history</button>`
      : "",
  ].filter(Boolean).join("") || `<div class="muted text-center p-3">Type a song name or paste a YouTube link</div>`;
  paintStatus(f ? `Suggestions for “${esc(f)}”` : "Search history & recommendations");
}

async function loadPopular() {
  try {
    popular = await api.searchSuggest("", 8);
  } catch { popular = []; }
}

/** Close the chip panel and paint results in the main home feed. */
async function runSearch(q, { force = false, replace = false } = {}) {
  const query = q.trim();
  if (query.length < 2) return;
  const music = !!getUiPrefs().omnibarMusic;
  if (!force && lastQ.toLowerCase() === query.toLowerCase() && lastMusic === music) return;
  lastQ = query;
  lastMusic = music;
  pushHistory(query);
  setOpen(false);
  mode = "idle";
  const si = $("#search-input");
  if (si) si.value = query;
  await feedSearch(query, 1, { replace, music });
}

const debouncedSearch = debounce((q) => {
  if (extractUrls(q).length) return;
  const t = q.trim();
  if (t.length >= 2) runSearch(t, { replace: true });
  else showHome(t);
}, 450);

const debouncedSuggest = debounce(async (q) => {
  const t = q.trim();
  if (t.length < 1 || extractUrls(q).length) return;
  try {
    const remote = await api.searchSuggest(t, 6);
    if (input().value.trim() !== t) return;
    popular = [...new Set([...remote, ...popular])];
    if (mode === "idle" && open && t.length < 2) showHome(t);
  } catch { /* ignore */ }
}, 280);

function handleSubmit(e) {
  e?.preventDefault?.();
  const raw = input().value.trim();
  if (!raw) { showHome(); setOpen(true); return; }
  const urls = extractUrls(raw);
  if (urls.length) {
    setOpen(false);
    acceptLinks(raw);
    toast(urls.length === 1 ? "Link ready — pick format & download" : `${urls.length} links ready`, "ok", { ms: 2600 });
    return;
  }
  // Force re-run even if same query (user pressed Enter)
  lastQ = "";
  runSearch(raw, { force: true, replace: false });
}

function onDocPointer(e) {
  if (!open) return;
  const wrap = $("#omnibar");
  if (wrap && !wrap.contains(e.target)) setOpen(false);
}

export function initOmnibar() {
  const form = $("#omnibar-form");
  if (!form) return;

  form.addEventListener("submit", handleSubmit);
  input().addEventListener("focus", () => {
    const v = input().value.trim();
    // Only open the chip panel when empty / short — live search goes to the main feed.
    if (v.length < 2) {
      setOpen(true);
      showHome(input().value);
    } else {
      setOpen(false);
    }
  });
  input().addEventListener("input", () => {
    const v = input().value;
    const urls = extractUrls(v);
    if (urls.length) {
      mode = "links";
      setOpen(true);
      home().hidden = true;
      paintStatus(`<i class="bi bi-link-45deg"></i> ${urls.length} YouTube link${urls.length === 1 ? "" : "s"} — press Enter`);
      host().innerHTML = `<div class="omnibar-link-hint">
        <i class="bi bi-youtube" style="font-size:22px;color:var(--g1)"></i>
        <div><b>${urls.length} link${urls.length === 1 ? "" : "s"} detected</b>
        <div class="faint small">Press Enter to preview &amp; download</div></div>
        <button type="submit" class="btn-grad sm" form="omnibar-form"><i class="bi bi-arrow-return-left"></i>Open</button>
      </div>`;
      return;
    }
    if (v.trim().length >= 2) {
      setOpen(false); // results live in the main feed, not this card
      debouncedSearch(v);
      debouncedSuggest(v);
    } else {
      showHome(v);
      setOpen(true);
    }
  });
  input().addEventListener("keydown", (e) => {
    if (e.key === "Escape") { setOpen(false); input().blur(); document.body.classList.remove("omnibar-focus"); }
  });

  // Music toggles live in prefs.js; re-search only when the Music pref actually flips.
  window.addEventListener("ui-prefs", (e) => {
    const music = !!e.detail?.omnibarMusic;
    if (music === lastMusic) return;
    if (lastQ.trim().length < 2 || extractUrls(input().value).length) {
      lastMusic = music;
      return;
    }
    lastMusic = music;
    lastQ = "";
    runSearch(input().value.trim() || lastQ, { force: true, replace: true });
  });
  $("#omnibar-expand")?.addEventListener("click", () => {
    document.body.classList.add("omnibar-focus");
    setTimeout(() => input().focus(), 30);
  });
  $("#omnibar-collapse")?.addEventListener("click", () => {
    document.body.classList.remove("omnibar-focus");
    setOpen(false);
  });

  home()?.addEventListener("click", (e) => {
    if (e.target.closest("[data-clear-hist]")) {
      store(HIST_KEY, []);
      showHome(input().value);
      return;
    }
    const chip = e.target.closest("[data-hist], [data-sug]");
    if (!chip) return;
    const q = chip.getAttribute("data-hist") || chip.getAttribute("data-sug");
    input().value = q;
    lastQ = "";
    runSearch(q, { force: true });
  });

  document.addEventListener("pointerdown", onDocPointer);
  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      document.body.classList.add("omnibar-focus");
      input().focus();
      input().select();
      if (input().value.trim().length < 2) {
        setOpen(true);
        showHome(input().value);
      }
    }
  });

  const kbd = $(".omnibar-kbd");
  if (kbd && !/Mac|iPhone|iPad/.test(navigator.platform || "")) kbd.textContent = "Ctrl K";

  window.addEventListener("focus-omnibar", () => {
    document.body.classList.add("omnibar-focus");
    input().focus();
  });

  loadPopular();
}
