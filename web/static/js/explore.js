// Home Explore shelf — For you / Music / Videos, or Search results in the same panel.
// Signed-in: always shown. Anonymous trending: only with Settings → ``exploreAnon`` (experimental).
// Search (omnibar / composer / URL ``?tab=search&q=``) paints here — never a dropdown.

import { api } from "./api.js";
import { navigateTo } from "./router.js";
import { handleResultClick, renderResultsHTML } from "./composer.js";
import { getUiPrefs } from "./prefs.js";
import { $, $$, esc, maybeCookieBanner, pagerHTML } from "./util.js";

let entries = [];
let kind = "auto";
let loaded = false;
let loading = false;
let seq = 0;
let lastSignedIn = false;
let page = 1;
let pages = 1;
const PAGE_SIZE = 12;

/** ``explore`` = trending/for-you shelf; ``search`` = query results in the same host. */
let mode = "explore";
let searchQ = "";
let searchPage = 1;
let searchPages = 1;
let searchSeq = 0;

function skeleton() {
  return Array.from({ length: 5 }, (_, i) =>
    `<div class="result" style="animation-delay:${i * 40}ms"><div class="skeleton" style="width:92px;height:52px"></div><div style="flex:1"><div class="skeleton" style="height:13px;width:80%;margin-bottom:7px"></div><div class="skeleton" style="height:11px;width:40%"></div></div></div>`
  ).join("");
}

const anonAllowed = () => !!getUiPrefs().exploreAnon;

function setSegFromSource(source) {
  kind = source === "for_you" ? "for_you" : source === "trending_videos" ? "videos" : "music";
  $$("#explore-seg [data-ex]").forEach((b) => b.classList.toggle("active", b.dataset.ex === kind));
}

function setVisible(on) {
  const box = $("#home-explore");
  if (box) box.hidden = !on;
}

function paintChrome({ title, sub, searchMode }) {
  const titleEl = $("#explore-title");
  const subEl = $("#explore-sub");
  const seg = $("#explore-seg");
  const refresh = $("#explore-refresh");
  const clear = $("#explore-clear-search");
  if (titleEl) titleEl.textContent = title;
  if (subEl) subEl.textContent = sub;
  if (seg) seg.hidden = !!searchMode;
  if (refresh) refresh.hidden = !!searchMode;
  if (clear) clear.hidden = !searchMode;
  document.getElementById("home-explore")?.classList.toggle("is-search", !!searchMode);
}

function paintBetaHint(signedIn) {
  const hint = $("#explore-beta");
  if (hint) hint.hidden = mode === "search" || !!signedIn || !anonAllowed();
}

function syncSearchInput(q) {
  const box = $("#search-input");
  if (box && box.value.trim() !== q) box.value = q;
  const omni = $("#omnibar-input");
  if (omni && document.activeElement !== omni && omni.value.trim() !== q) omni.value = q;
}

/**
 * Run a YouTube search and paint results into the main home feed (same panel as Explore).
 * Pass ``results`` to skip a network round-trip (e.g. after a prior fetch).
 */
export async function feedSearch(q, nextPage = 1, {
  fromRoute = false,
  replace = false,
  results = null,
  pages: pagesIn = null,
  music = null,
} = {}) {
  const query = String(q || "").trim();
  if (query.length < 2) return;
  const want = Math.max(1, Math.min(10, Number(nextPage) || 1));
  const useMusic = music != null ? !!music : !!getUiPrefs().omnibarMusic;
  const my = ++searchSeq;
  mode = "search";
  searchQ = query;
  searchPage = want;
  syncSearchInput(query);
  setVisible(true);
  paintBetaHint(true);
  paintChrome({
    title: "Search results",
    sub: `“${query}” · loading…`,
    searchMode: true,
  });

  if (!fromRoute) {
    navigateTo(
      { page: "home", tab: "search", q: query, sp: want, explore: "", ep: 1 },
      { replace, reason: "search" },
    );
  }

  const host = $("#explore-results");
  if (!host) return;

  if (results) {
    if (my !== searchSeq) return;
    entries = results;
    searchPages = Math.max(1, Math.min(10, Number(pagesIn) || 1));
    paintChrome({
      title: "Search results",
      sub: entries.length
        ? `“${query}” · ${entries.length} result${entries.length === 1 ? "" : "s"}${searchPages > 1 ? ` · page ${searchPage}/${searchPages}` : ""}`
        : `“${query}” · no matches`,
      searchMode: true,
    });
    host.innerHTML = entries.length
      ? renderResultsHTML(entries) + pagerHTML({ page: searchPage, pages: searchPages })
      : `<div class="muted text-center p-3">No results.</div>`;
    loaded = true;
    return;
  }

  host.innerHTML = skeleton();
  try {
    const data = await api.search(query, useMusic, { page: want, limit: PAGE_SIZE });
    if (my !== searchSeq) return;
    entries = data.entries || [];
    searchPage = data.page || want;
    searchPages = data.pages || 1;
    paintChrome({
      title: "Search results",
      sub: entries.length
        ? `“${query}” · ${entries.length} result${entries.length === 1 ? "" : "s"}${searchPages > 1 ? ` · page ${searchPage}/${searchPages}` : ""}`
        : `“${query}” · no matches`,
      searchMode: true,
    });
    host.innerHTML = entries.length
      ? renderResultsHTML(entries) + pagerHTML({ page: searchPage, pages: searchPages })
      : `<div class="muted text-center p-3">No results.</div>`;
    loaded = true;
  } catch (err) {
    if (my !== searchSeq) return;
    entries = [];
    const msg = err.detail || err.message;
    maybeCookieBanner(msg);
    paintChrome({ title: "Search results", sub: `“${query}” · failed`, searchMode: true });
    host.innerHTML = `<div class="job-error"><i class="bi bi-exclamation-octagon-fill"></i><span>${esc(msg)}</span></div>`;
  }
}

/** Leave search mode and reload the explore shelf. */
export function clearFeedSearch({ kind: nextKind } = {}) {
  mode = "explore";
  searchQ = "";
  searchPage = 1;
  searchSeq += 1; // cancel in-flight search paints
  const preferred = nextKind || getUiPrefs().exploreDefault || "auto";
  load(preferred === "auto" ? "for_you" : preferred, 1, { replace: false });
}

async function load(nextKind = kind, nextPage = 1, { fromRoute = false, replace = false } = {}) {
  const my = ++seq;
  mode = "explore";
  kind = nextKind || "auto";
  page = Math.max(1, Math.min(10, Number(nextPage) || 1));
  const host = $("#explore-results");
  if (!host) return;
  loading = true;
  setVisible(true);
  paintChrome({ title: "Explore", sub: "Loading…", searchMode: false });
  host.innerHTML = skeleton();
  try {
    const feed = await api.explore(kind === "auto" ? "auto" : kind, { page, limit: PAGE_SIZE });
    if (my !== seq || mode !== "explore") return;
    lastSignedIn = !!feed.signed_in;

    // Unsigned + experimental off → hide (for_you probe returns empty quickly, no trending storm).
    if (!feed.signed_in && !anonAllowed()) {
      entries = [];
      loaded = true;
      setVisible(false);
      return;
    }

    setVisible(true);
    paintBetaHint(feed.signed_in);
    entries = feed.entries || [];
    page = feed.page || page;
    pages = feed.pages || 1;
    setSegFromSource(feed.source);
    paintChrome({
      title: feed.title || "Explore",
      sub: feed.message
        ? feed.message
        : (feed.source === "for_you"
          ? `Recommended for your YouTube account${pages > 1 ? ` · page ${page}/${pages}` : ""}`
          : (feed.signed_in
            ? `Popular picks · play, cart or download${pages > 1 ? ` · page ${page}/${pages}` : ""}`
            : `Experimental · may fail without cookies${pages > 1 ? ` · page ${page}/${pages}` : ""}`)),
      searchMode: false,
    });
    if (!entries.length) {
      const needCk = feed.source === "for_you" && !feed.signed_in;
      host.innerHTML = `<div class="glass-in p-3 text-center">
        <div class="mb-2"><i class="bi bi-${needCk ? "person-heart" : "compass"}" style="font-size:26px;color:var(--g2)"></i></div>
        <div class="small mb-2">${esc(feed.message || "Nothing to show right now.")}</div>
        <button type="button" class="btn-grad sm" data-ex-cookies><i class="bi bi-key-fill me-1"></i>${needCk ? "Add cookies" : "Fix cookies"}</button>
        ${needCk && anonAllowed()
          ? `<button type="button" class="btn-ghost sm ms-2" data-ex-fallback="music"><i class="bi bi-music-note-beamed"></i>Trending music</button>`
          : ""}
      </div>`;
    } else {
      host.innerHTML = renderResultsHTML(entries) + pagerHTML({ page, pages });
    }
    loaded = true;
    if (!fromRoute) {
      const ex = (kind === "for_you" || kind === "music" || kind === "videos") ? kind : "";
      navigateTo(
        { page: "home", explore: ex, ep: page, tab: "link", q: "", sp: 1 },
        { replace, reason: "explore" },
      );
    }
  } catch (err) {
    if (my !== seq || mode !== "explore") return;
    if (!anonAllowed() && !lastSignedIn) {
      setVisible(false);
      return;
    }
    setVisible(true);
    paintBetaHint(lastSignedIn);
    paintChrome({ title: "Explore", sub: "Could not load", searchMode: false });
    host.innerHTML = `<div class="job-error"><i class="bi bi-exclamation-octagon-fill"></i><span>${esc(err.detail || err.message)}</span></div>`;
  } finally {
    if (my === seq) loading = false;
  }
}

/** Load when Home is visible (first visit or after cookies / prefs change). */
export function refreshExplore({ force = false } = {}) {
  if (document.body.dataset.page !== "home") return;
  // Don't clobber an active / URL-restored search.
  if (mode === "search" && searchQ && !force) return;
  if (!force && loaded && entries.length && mode === "explore") return;
  if (loading && !force) return;

  // Without the experimental anon flag, only probe login via for_you / auto→for_you when signed in.
  // Using for_you when unsigned avoids kicking off Music search queries.
  if (!anonAllowed() && !lastSignedIn) {
    load("for_you", 1, { replace: true });
    return;
  }

  const preferred = getUiPrefs().exploreDefault || "auto";
  const next = (kind === "for_you" || kind === "music" || kind === "videos") && loaded ? kind : preferred;
  load(next, force ? 1 : page, { replace: true });
}

export function initExplore() {
  const box = $("#home-explore");
  if (!box) return;
  box.hidden = true;

  // Clear-search control (injected once next to refresh)
  const headActions = box.querySelector(".page-head .flex.items-center");
  if (headActions && !$("#explore-clear-search")) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "explore-clear-search";
    btn.className = "btn-ghost sm";
    btn.hidden = true;
    btn.innerHTML = `<i class="bi bi-x-lg"></i>Clear search`;
    btn.addEventListener("click", () => clearFeedSearch());
    headActions.appendChild(btn);
  }

  window.addEventListener("route", (e) => {
    const r = e.detail || {};
    if (r.page !== "home") return;
    if (r.reason !== "popstate" && r.reason !== "init") return;
    // Search URL wins over explore shelf.
    if (r.tab === "search" && r.q && r.q.length >= 2) {
      feedSearch(r.q, r.sp || 1, { fromRoute: true });
      return;
    }
    if (r.explore === "music" || r.explore === "videos" || r.explore === "for_you") {
      load(r.explore, r.ep || 1, { fromRoute: true });
    }
  });
  $("#explore-seg")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-ex]");
    if (!b) return;
    load(b.dataset.ex, 1, { replace: false });
  });
  $("#explore-refresh")?.addEventListener("click", () => load(kind, page, { replace: true }));
  $("#explore-results")?.addEventListener("click", (e) => {
    const pg = e.target.closest(".results-pager [data-page]");
    if (pg) {
      e.preventDefault(); e.stopPropagation();
      if (pg.disabled) return;
      if (mode === "search") feedSearch(searchQ, Number(pg.dataset.page), { replace: false });
      else load(kind, Number(pg.dataset.page), { replace: false });
      return;
    }
    if (e.target.closest("[data-ex-cookies]")) {
      window.dispatchEvent(new CustomEvent("open-cookies"));
      return;
    }
    const fb = e.target.closest("[data-ex-fallback]");
    if (fb) { load(fb.dataset.exFallback, 1); return; }
    handleResultClick(e, () => entries);
  });
  window.addEventListener("cookies-changed", () => {
    loaded = false;
    entries = [];
    lastSignedIn = false;
    page = 1;
    if (mode === "search" && searchQ) feedSearch(searchQ, searchPage, { fromRoute: true, replace: true });
    else refreshExplore({ force: true });
  });
  window.addEventListener("ui-prefs", () => {
    if (mode === "search" && searchQ) {
      feedSearch(searchQ, searchPage, { replace: true });
      return;
    }
    loaded = false;
    page = 1;
    refreshExplore({ force: true });
  });
  window.addEventListener("feed-search", (e) => {
    const d = e.detail || {};
    if (!d.q) return;
    feedSearch(d.q, d.page || 1, {
      fromRoute: !!d.fromRoute,
      replace: !!d.replace,
      results: d.results || null,
      pages: d.pages,
      music: d.music,
    });
  });
  window.addEventListener("player-change", (e) => {
    document.querySelectorAll("#explore-results .result").forEach((el) => {
      el.classList.toggle("is-playing", !!e.detail.key && el.dataset.key === e.detail.key);
    });
  });
}
