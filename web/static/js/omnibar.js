// Universal top-bar search: paste links OR type a query.
// History (local), popular suggestions (API), debounce, opaque animated panel.

import { api } from "./api.js";
import { acceptLinks, acceptSearchQuery, handleResultClick, renderResultsHTML } from "./composer.js";
import { getUiPrefs } from "./prefs.js";
import { toast } from "./ui.js";
import { $, debounce, esc, extractUrls, maybeCookieBanner, store } from "./util.js";

const HIST_KEY = "mst.searchHistory";
const CACHE_KEY = "mst.searchCache";
const HIST_MAX = 12;
const CACHE_TTL_MS = 10 * 60 * 1000; // mirror typical SERVER_CACHE_MINUTES client-side

let open = false;
let results = [];
let lastQ = "";
let lastMusic = null;  // last Music-pref we searched with (ui-prefs re-run only on flip)
let seq = 0;
let inflightKey = null;  // `${music}:${query}` while a network search is outstanding
let popular = [];
let mode = "idle"; // idle | links | results

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

function cacheGet(q, music) {
  const all = store(CACHE_KEY) || {};
  const hit = all[`${music ? "m" : "y"}:${q.trim().toLowerCase()}`];
  if (!hit || Date.now() - hit.at > CACHE_TTL_MS) return null;
  return hit.items;
}

function cacheSet(q, music, items) {
  const all = store(CACHE_KEY) || {};
  const key = `${music ? "m" : "y"}:${q.trim().toLowerCase()}`;
  all[key] = { at: Date.now(), items };
  // keep last 20 keys
  const keys = Object.keys(all);
  if (keys.length > 20) {
    keys.sort((a, b) => (all[a].at || 0) - (all[b].at || 0));
    keys.slice(0, keys.length - 20).forEach((k) => delete all[k]);
  }
  store(CACHE_KEY, all);
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

function skeleton() {
  return Array.from({ length: 4 }, (_, i) =>
    `<div class="result omni-anim" style="animation-delay:${i * 40}ms"><div class="skeleton" style="width:92px;height:52px"></div><div style="flex:1"><div class="skeleton" style="height:13px;width:80%;margin-bottom:7px"></div><div class="skeleton" style="height:11px;width:40%"></div></div></div>`
  ).join("");
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
  // de-dupe popular against history
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

async function runSearch(q, { syncComposer = true, force = false } = {}) {
  const query = q.trim();
  if (query.length < 2) return;
  const music = !!getUiPrefs().omnibarMusic;
  const key = `${music ? "m" : "y"}:${query.toLowerCase()}`;
  // Same query already on the wire / just painted — don't stampede /api/search.
  if (!force && (inflightKey === key || (lastQ.toLowerCase() === query.toLowerCase() && lastMusic === music && results.length))) {
    if (syncComposer && results.length) acceptSearchQuery(query, { music, submit: false, results });
    return;
  }
  lastQ = query;
  lastMusic = music;
  const my = ++seq;
  inflightKey = key;
  mode = "results";
  setOpen(true);
  home().hidden = true;
  pushHistory(query);

  const cached = cacheGet(query, music);
  if (cached) {
    results = cached;
    host().innerHTML = renderResultsHTML(results);
    paintStatus(`${results.length} results · cached`);
    if (syncComposer) acceptSearchQuery(query, { music, submit: false, results });
    if (my === seq) inflightKey = null;
    return;
  }

  paintStatus(`Searching “${esc(query)}”…`);
  host().innerHTML = skeleton();
  // Mirror query into the composer Search tab without a second network round-trip.
  if (syncComposer) acceptSearchQuery(query, { music, submit: false });
  try {
    const data = await api.search(query, music, { page: 1, limit: 12 });
    if (my !== seq) return;
    results = data.entries || [];
    cacheSet(query, music, results);
    if (!results.length) {
      host().innerHTML = `<div class="muted text-center p-3">No results.</div>`;
      paintStatus("No matches");
      return;
    }
    host().innerHTML = renderResultsHTML(results);
    paintStatus(`${results.length} result${results.length === 1 ? "" : "s"} · Play, cart or download`);
    if (syncComposer) acceptSearchQuery(query, {
      music, submit: false, results, page: data.page || 1, pages: data.pages || 1,
    });
  } catch (err) {
    if (my !== seq) return;
    results = [];
    const msg = err.detail || err.message;
    maybeCookieBanner(msg);
    host().innerHTML = `<div class="job-error"><i class="bi bi-exclamation-octagon-fill"></i><span>${esc(msg)}</span></div>`;
    paintStatus("Search failed");
  } finally {
    if (my === seq) inflightKey = null;
  }
}

const debouncedSearch = debounce((q) => {
  if (extractUrls(q).length) return;
  const t = q.trim();
  if (t.length >= 2) runSearch(t);
  else showHome(t);
}, 450);

const debouncedSuggest = debounce(async (q) => {
  const t = q.trim();
  if (t.length < 1 || extractUrls(q).length) return;
  try {
    const remote = await api.searchSuggest(t, 6);
    if (input().value.trim() !== t) return;
    popular = [...new Set([...remote, ...popular])];
    if (mode === "idle") showHome(t);
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
  runSearch(raw);
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
    setOpen(true);
    if (!input().value.trim() || mode === "idle") showHome(input().value);
    else if (results.length) { home().hidden = true; }
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
      showHome(v); // instant local suggestions while debounce fires search
      debouncedSearch(v);
      debouncedSuggest(v);
    } else {
      results = [];
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
    runSearch(lastQ, { force: true });
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
    runSearch(q);
  });
  host()?.addEventListener("click", (e) => handleResultClick(e, () => results));

  document.addEventListener("pointerdown", onDocPointer);
  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      document.body.classList.add("omnibar-focus");
      input().focus();
      input().select();
      setOpen(true);
      showHome(input().value);
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
