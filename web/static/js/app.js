// Bootstrap: path-based pages (/ · /download · /library), live updates, health, modules.
// Same features as before — Downloads and Library are full pages; Home is Find & download.

import { api } from "./api.js";
import { initCart } from "./cart.js";
import { initComposer, refreshFolders } from "./composer.js";
import { initOmnibar } from "./omnibar.js";
import { initExplore, refreshExplore } from "./explore.js";
import { initAmbient } from "./ambient.js";
import { initEqualizer } from "./equalizer.js";
import { initPlayer } from "./player.js";
import { initStage } from "./stage.js";
import { initLibrary, loadLibrary, currentPath } from "./library.js";
import { applySnapshot, initQueue, setParCap, setQueueMode, showRuntime } from "./queue.js";
import { initConnect } from "./connect.js";
import { initDeliveries } from "./deliveries.js";
import { initSubscriptions } from "./subscriptions.js";
import { initSettings, renderHealth } from "./settings.js";
import { initStatus, paintMode as paintStatusMode, setLive as paintStatusLive } from "./status.js";
import { initTheme } from "./theme.js";
import { getUiPrefs, initUiPrefs } from "./prefs.js";
import { connectEvents } from "./sse.js";
import { enableRipple, initTooltips, toast } from "./ui.js";
import { $, $$, hideCookieBanner } from "./util.js";

/** page id → path */
const PAGES = {
  home: "/",
  download: "/download",
  library: "/library",
};
const PATH_TO_PAGE = {
  "/": "home",
  "/download": "download",
  "/downloads": "download",  // friendly alias
  "/library": "library",
};
const HASH_LEGACY = { new: "home", queue: "download", library: "library", home: "home", download: "download" };

let page = "home";
let completedSeen = null;

function pathOf(p) { return PAGES[p] || "/"; }

function pageFromLocation() {
  const raw = (location.pathname || "/").replace(/\/+$/, "") || "/";
  if (PATH_TO_PAGE[raw]) return PATH_TO_PAGE[raw];
  // old bookmarks: /#queue → download, etc.
  const h = location.hash.replace(/^#/, "");
  if (HASH_LEGACY[h]) return HASH_LEGACY[h];
  return "home";
}

function applyPage() {
  document.body.dataset.page = page;
  const show = {
    home: $("#panel-new"),
    download: $("#panel-queue"),
    library: $("#panel-library"),
  };
  Object.entries(show).forEach(([id, el]) => { if (el) el.hidden = id !== page; });
  $$("#page-nav [data-page], #bottom-nav [data-page]").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === page);
  });
  if (page === "library") loadLibrary(currentPath());
  if (page === "home") refreshExplore();
}

/** Navigate between Home / Downloads / Library. Uses History API; server serves index for each path. */
export function navigate(next, { replace = false, path = null, scroll = true } = {}) {
  if (!PAGES[next]) next = "home";
  page = next;
  applyPage();
  const url = path || pathOf(next);
  const cur = location.pathname.replace(/\/+$/, "") || "/";
  const dest = url.replace(/\/+$/, "") || "/";
  if (replace || cur === dest) history.replaceState({ page }, "", url);
  else history.pushState({ page }, "", url);
  if (scroll) window.scrollTo({ top: 0, behavior: "smooth" });
}

/** Mode → status menu + library tips. */
function paintMode(cfg) {
  const server = !!cfg.server_mode;
  const ttl = cfg.delivery_ttl_minutes || 60;
  paintStatusMode(cfg);
  const libTip = $("#lib-mode-tip");
  if (libTip) {
    libTip.hidden = !server;
    libTip.innerHTML = server
      ? `<i class="bi bi-info-circle"></i> Look at <b>Your downloads</b> at the top for songs you just finished (kept ~${ttl} min) — press <b>Save to device</b>. The folders below (Normal Albums, English, …) are the operator’s shared library, not your private downloads.`
      : "";
  }
  const hostLabel = $("#lib-host-label");
  if (hostLabel) hostLabel.hidden = !server;
}

function paintMaintenance(cfg) {
  const on = !!(cfg?.server_mode && cfg.maintenance);
  $("#maint-banner").hidden = !on;
  $("#maint-banner-text").textContent = on ? (cfg.maintenance_message || "Please try again later.") : "";
}

async function pollMaintenance() {
  if (!document.body.classList.contains("server-mode")) return;
  try { paintMaintenance(await api.config()); } catch { /* health pill covers outages */ }
}

async function pollHealth() {
  try { renderHealth(await api.health()); } catch { renderHealth(null); }
}

function setLive(state) {
  paintStatusLive(state);
}

function onNavClick(e) {
  const a = e.target.closest("[data-page]");
  if (!a || !a.dataset.page) return;
  // allow modified clicks to open a real new tab
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button === 1) return;
  e.preventDefault();
  navigate(a.dataset.page);
}

async function boot() {
  initTheme();
  initUiPrefs();  // browser-local prefs (appearance, auto-save, …) before UI paints
  enableRipple();
  initTooltips();
  initStatus();
  initQueue();
  initLibrary();
  initDeliveries();
  initSettings();
  initConnect();
  initPlayer();
  initStage();
  initAmbient();
  initEqualizer();
  $("#cookie-banner-fix")?.addEventListener("click", () => window.dispatchEvent(new CustomEvent("open-cookies")));
  $("#cookie-banner-dismiss")?.addEventListener("click", hideCookieBanner);
  window.addEventListener("cookies-changed", hideCookieBanner);
  window.addEventListener("open-settings", () => $("#open-settings")?.click());

  // pages
  $("#page-nav")?.addEventListener("click", onNavClick);
  $("#bottom-nav")?.addEventListener("click", onNavClick);
  document.querySelectorAll(".page-head [data-page]").forEach((a) => a.addEventListener("click", onNavClick));
  window.addEventListener("popstate", () => { page = pageFromLocation(); applyPage(); });
  window.addEventListener("go-queue", () => {
    if (getUiPrefs().openQueueOnSubmit) navigate("download");
  });
  window.addEventListener("go-new", () => navigate("home", { scroll: true }));
  window.addEventListener("open-folder", (e) => {
    navigate("library", { replace: false });
    loadLibrary(e.detail || "");
  });

  // initial route (path wins; legacy #queue / #library still work once)
  page = pageFromLocation();
  applyPage();
  history.replaceState({ page }, "", pathOf(page));

  try {
    const [cfg, rt] = await Promise.all([api.config(), api.runtime()]);
    document.body.classList.toggle("server-mode", !!cfg.server_mode);
    paintMode(cfg);
    paintMaintenance(cfg);
    setQueueMode(cfg);
    initComposer(cfg);
    initOmnibar();
    initExplore();
    // Home's applyPage already refreshes Explore; only force when we landed on Home after config.
    if (page === "home") refreshExplore({ force: true });
    initCart(cfg);
    initSubscriptions(cfg);
    setParCap(cfg.max_parallel_cap);
    showRuntime(rt);
    $("#s-par").max = cfg.max_parallel_cap;
  } catch (e) {
    toast(e.detail || "Could not load configuration", "err", { title: "Backend unreachable" });
  }

  connectEvents({
    onStatus: setLive,
    onSnapshot: (snap) => {
      applySnapshot(snap);
      const done = snap.stats?.completed;
      if (done != null) {
        if (completedSeen != null && done > completedSeen) {
          if (page === "library") loadLibrary(currentPath());
          refreshFolders();
        }
        completedSeen = done;
      }
    },
  });

  pollHealth();
  setInterval(pollHealth, 15000);
  setInterval(pollMaintenance, 60000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollHealth(); pollMaintenance(); } });
}

boot();
