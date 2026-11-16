/**
 * Path + query router for the SPA shell.
 *
 * Paths (served by FastAPI as the same index.html):
 *   /              Home (Find & download + Explore)
 *   /download      Downloads queue  (/downloads alias)
 *   /library       Library browser
 *
 * Query (Home):
 *   tab=link|search|mine     composer source (mine → account)
 *   q=…                      search text
 *   sp=1..10                 search results page
 *   explore=music|videos|for_you   explore shelf (omit = default/auto)
 *   ep=1..10                 explore page
 *
 * Query (Library):
 *   path=rel/folder          folder under downloads/
 *
 * Backend /api/* URLs are unchanged — this only drives UI history so
 * Back/Forward restore tabs, search, explore and library folders.
 */

const PAGES = {
  home: "/",
  download: "/download",
  library: "/library",
};
const PATH_TO_PAGE = {
  "/": "home",
  "/download": "download",
  "/downloads": "download",
  "/library": "library",
};
const HASH_LEGACY = { new: "home", queue: "download", library: "library", home: "home", download: "download" };

const TABS = new Set(["link", "search", "mine"]);
const EXPLORE = new Set(["music", "videos", "for_you"]);

let applying = false; // ignore outbound writes while applying an inbound route
let last = null;

function clampPage(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v < 1) return 1;
  return Math.min(10, Math.floor(v));
}

/** Internal composer src ↔ URL tab. */
export function tabFromSrc(src) {
  return src === "account" ? "mine" : (src === "search" ? "search" : "link");
}
export function srcFromTab(tab) {
  return tab === "mine" ? "account" : (tab === "search" ? "search" : "link");
}

export function parseLocation(loc = window.location) {
  const raw = (loc.pathname || "/").replace(/\/+$/, "") || "/";
  let page = PATH_TO_PAGE[raw];
  if (!page) {
    const h = (loc.hash || "").replace(/^#/, "");
    page = HASH_LEGACY[h] || "home";
  }
  const qs = new URLSearchParams(loc.search || "");
  const tabRaw = (qs.get("tab") || "link").toLowerCase();
  const tab = TABS.has(tabRaw) ? tabRaw : "link";
  const exploreRaw = (qs.get("explore") || "").toLowerCase();
  const explore = EXPLORE.has(exploreRaw) ? exploreRaw : "";
  return {
    page,
    tab: page === "home" ? tab : "link",
    q: page === "home" ? (qs.get("q") || "").trim() : "",
    sp: page === "home" ? clampPage(qs.get("sp") || 1) : 1,
    explore: page === "home" ? explore : "",
    ep: page === "home" ? clampPage(qs.get("ep") || 1) : 1,
    path: page === "library" ? (qs.get("path") || "").replace(/^\/+|\/+$/g, "") : "",
  };
}

export function buildUrl(route) {
  const r = { ...parseLocation(), ...route };
  const page = PAGES[r.page] ? r.page : "home";
  const path = PAGES[page];
  const qs = new URLSearchParams();
  if (page === "home") {
    const tab = TABS.has(r.tab) ? r.tab : "link";
    if (tab !== "link") qs.set("tab", tab);
    if (tab === "search" && r.q) {
      qs.set("q", r.q);
      const sp = clampPage(r.sp || 1);
      if (sp > 1) qs.set("sp", String(sp));
    }
    if (r.explore && EXPLORE.has(r.explore)) {
      qs.set("explore", r.explore);
      const ep = clampPage(r.ep || 1);
      if (ep > 1) qs.set("ep", String(ep));
    }
  } else if (page === "library" && r.path) {
    qs.set("path", r.path);
  }
  const q = qs.toString();
  return q ? `${path}?${q}` : path;
}

export function getRoute() {
  return last ? { ...last } : parseLocation();
}

function sameRoute(a, b) {
  if (!a || !b) return false;
  return a.page === b.page
    && a.tab === b.tab
    && a.q === b.q
    && a.sp === b.sp
    && a.explore === b.explore
    && a.ep === b.ep
    && a.path === b.path;
}

/**
 * Apply a route to the UI (panels) and notify listeners.
 * Does not touch history — caller owns push/replace.
 */
export function applyRoute(route, { reason = "apply" } = {}) {
  const next = {
    page: PAGES[route.page] ? route.page : "home",
    tab: TABS.has(route.tab) ? route.tab : "link",
    q: String(route.q || "").trim(),
    sp: clampPage(route.sp || 1),
    explore: EXPLORE.has(route.explore) ? route.explore : "",
    ep: clampPage(route.ep || 1),
    path: String(route.path || "").replace(/^\/+|\/+$/g, ""),
  };
  if (sameRoute(last, next) && reason !== "init") return next;
  last = next;
  applying = true;
  try {
    window.dispatchEvent(new CustomEvent("route", { detail: { ...next, reason } }));
  } finally {
    applying = false;
  }
  return next;
}

/**
 * Merge patch into the current route, update the address bar, apply UI.
 * @param {object} patch
 * @param {{ replace?: boolean, scroll?: boolean, reason?: string }} opts
 */
export function navigateTo(patch = {}, { replace = false, scroll = false, reason = "navigate" } = {}) {
  if (applying && reason !== "init") return getRoute();
  const merged = { ...getRoute(), ...patch };
  // Drop search params when leaving search tab
  if (merged.tab !== "search") {
    if (patch.tab && patch.tab !== "search") {
      merged.q = patch.q !== undefined ? patch.q : "";
      merged.sp = 1;
    }
  }
  if (merged.page !== "home") {
    merged.tab = "link";
    merged.q = "";
    merged.sp = 1;
    // keep explore out of non-home URLs
    merged.explore = "";
    merged.ep = 1;
  }
  if (merged.page !== "library") merged.path = "";

  const url = buildUrl(merged);
  const cur = `${location.pathname}${location.search}`.replace(/\/\?(?=$)/, "/") || "/";
  const dest = url;
  if (replace || cur === dest) history.replaceState({ route: merged }, "", url);
  else history.pushState({ route: merged }, "", url);

  const applied = applyRoute(merged, { reason });
  if (scroll) window.scrollTo({ top: 0, behavior: "smooth" });
  return applied;
}

/** replaceState helper for in-place sync (no history entry). */
export function replaceRoute(patch = {}, opts = {}) {
  return navigateTo(patch, { ...opts, replace: true, scroll: false, reason: opts.reason || "replace" });
}

/** Read the address bar and apply (Back/Forward / first paint). */
export function syncFromLocation({ reason = "popstate" } = {}) {
  const parsed = parseLocation();
  const url = buildUrl(parsed);
  // Normalize aliases (/downloads → /download) without losing query
  const cur = `${location.pathname}${location.search}`;
  if (cur !== url) history.replaceState({ route: parsed }, "", url);
  return applyRoute(parsed, { reason });
}

export function initRouter() {
  window.addEventListener("popstate", () => syncFromLocation({ reason: "popstate" }));
}

export const paths = { ...PAGES };
