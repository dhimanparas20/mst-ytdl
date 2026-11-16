// Home Explore shelf — For you / Music / Videos (paginated, max 10 pages).
// Signed-in: always shown. Anonymous trending: only with Settings → ``exploreAnon`` (experimental).

import { api } from "./api.js";
import { handleResultClick, renderResultsHTML } from "./composer.js";
import { getUiPrefs } from "./prefs.js";
import { $, $$, esc, pagerHTML } from "./util.js";

let entries = [];
let kind = "auto";
let loaded = false;
let loading = false;
let seq = 0;
let lastSignedIn = false;
let page = 1;
let pages = 1;
const PAGE_SIZE = 12;

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

function paintBetaHint(signedIn) {
  const hint = $("#explore-beta");
  if (hint) hint.hidden = !!signedIn || !anonAllowed();
}

async function load(nextKind = kind, nextPage = 1) {
  const my = ++seq;
  kind = nextKind || "auto";
  page = Math.max(1, Math.min(10, Number(nextPage) || 1));
  const host = $("#explore-results");
  const title = $("#explore-title");
  const sub = $("#explore-sub");
  if (!host) return;
  loading = true;
  host.innerHTML = skeleton();
  if (sub) sub.textContent = "Loading…";
  try {
    const feed = await api.explore(kind === "auto" ? "auto" : kind, { page, limit: PAGE_SIZE });
    if (my !== seq) return;
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
    if (title) title.textContent = feed.title || "Explore";
    if (sub) {
      const pageNote = pages > 1 ? ` · page ${page}/${pages}` : "";
      sub.textContent = feed.message
        ? feed.message
        : (feed.source === "for_you"
          ? `Recommended for your YouTube account${pageNote}`
          : (feed.signed_in
            ? `Popular picks · play, cart or download${pageNote}`
            : `Experimental · may fail without cookies${pageNote}`));
    }
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
  } catch (err) {
    if (my !== seq) return;
    if (!anonAllowed() && !lastSignedIn) {
      setVisible(false);
      return;
    }
    setVisible(true);
    paintBetaHint(lastSignedIn);
    host.innerHTML = `<div class="job-error"><i class="bi bi-exclamation-octagon-fill"></i><span>${esc(err.detail || err.message)}</span></div>`;
    if (sub) sub.textContent = "Could not load";
  } finally {
    if (my === seq) loading = false;
  }
}

/** Load when Home is visible (first visit or after cookies / prefs change). */
export function refreshExplore({ force = false } = {}) {
  if (document.body.dataset.page !== "home") return;
  if (!force && loaded && entries.length) return;
  if (loading && !force) return;

  // Without the experimental anon flag, only probe login via for_you / auto→for_you when signed in.
  // Using for_you when unsigned avoids kicking off Music search queries.
  if (!anonAllowed() && !lastSignedIn) {
    load("for_you", 1);
    return;
  }

  const preferred = getUiPrefs().exploreDefault || "auto";
  const next = (kind === "for_you" || kind === "music" || kind === "videos") && loaded ? kind : preferred;
  load(next, force ? 1 : page);
}

export function initExplore() {
  const box = $("#home-explore");
  if (!box) return;
  box.hidden = true;
  $("#explore-seg")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-ex]");
    if (!b) return;
    load(b.dataset.ex, 1);
  });
  $("#explore-refresh")?.addEventListener("click", () => load(kind, page));
  $("#explore-results")?.addEventListener("click", (e) => {
    const pg = e.target.closest(".results-pager [data-page]");
    if (pg) {
      e.preventDefault(); e.stopPropagation();
      if (!pg.disabled) load(kind, Number(pg.dataset.page));
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
    refreshExplore({ force: true });
  });
  window.addEventListener("ui-prefs", () => {
    loaded = false;
    page = 1;
    refreshExplore({ force: true });
  });
  window.addEventListener("player-change", (e) => {
    document.querySelectorAll("#explore-results .result").forEach((el) => {
      el.classList.toggle("is-playing", !!e.detail.key && el.dataset.key === e.detail.key);
    });
  });
}
