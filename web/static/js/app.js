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
import { initRouter, navigateTo, paths, syncFromLocation, getRoute, replaceRoute } from "./router.js";
import { $, $$, hideCookieBanner } from "./util.js";

let completedSeen = null;

function paintPageChrome(page) {
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
}

/** Navigate between Home / Downloads / Library (path + query preserved via router). */
export function navigate(next, { replace = false, scroll = true, ...patch } = {}) {
  const page = paths[next] ? next : "home";
  return navigateTo({ page, ...patch }, { replace, scroll, reason: "nav" });
}

function onRoute(e) {
  const r = e.detail || {};
  paintPageChrome(r.page || "home");
  // Module-specific restores listen on the same ``route`` event.
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
  navigate(a.dataset.page, { scroll: true });
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

  // pages — path + query router (Back/Forward restores tabs / search / explore / library folder)
  initRouter();
  window.addEventListener("route", onRoute);
  $("#page-nav")?.addEventListener("click", onNavClick);
  $("#bottom-nav")?.addEventListener("click", onNavClick);
  document.querySelectorAll(".page-head [data-page]").forEach((a) => a.addEventListener("click", onNavClick));
  window.addEventListener("go-queue", () => {
    if (getUiPrefs().openQueueOnSubmit) navigate("download", { scroll: true });
  });
  window.addEventListener("go-new", () => navigate("home", { scroll: true }));
  window.addEventListener("open-folder", (e) => {
    navigate("library", { path: e.detail || "", scroll: true });
  });

  // Show the right panel immediately (full query restore runs after module inits below)
  paintPageChrome(getRoute().page || "home");

  try {
    const [cfg, rt] = await Promise.all([api.config(), api.runtime()]);
    document.body.classList.toggle("server-mode", !!cfg.server_mode);
    paintMode(cfg);
    paintMaintenance(cfg);
    setQueueMode(cfg);
    initComposer(cfg);
    initOmnibar();
    initExplore();
    // Apply path + query now that listeners are registered (tab / q / explore / library path).
    syncFromLocation({ reason: "init" });
    // Default Explore shelf only when the URL is not already a search / explore restore.
    const r0 = getRoute();
    const searching = r0.tab === "search" && r0.q && r0.q.length >= 2;
    if (searching && r0.explore) {
      // Drop stale explore= from a shared/bookmarked URL so refresh matches the feed.
      replaceRoute({ explore: "", ep: 1 }, { reason: "search-normalize" });
    }
    if (r0.page === "home" && !r0.explore && !searching) refreshExplore({ force: true });
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
          if (getRoute().page === "library") loadLibrary(currentPath());
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
