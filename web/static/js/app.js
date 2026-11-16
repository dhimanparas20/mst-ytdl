// Bootstrap: wires tabs, live updates, health polling and the feature modules.

import { api } from "./api.js";
import { initCart } from "./cart.js";
import { initComposer, refreshFolders } from "./composer.js";
import { initPlayer } from "./player.js";
import { initStage } from "./stage.js";
import { initLibrary, loadLibrary, currentPath } from "./library.js";
import { applySnapshot, initQueue, setParCap, setQueueMode, showRuntime } from "./queue.js";
import { initConnect } from "./connect.js";
import { initDeliveries } from "./deliveries.js";
import { initSubscriptions } from "./subscriptions.js";
import { initSettings, renderHealth } from "./settings.js";
import { connectEvents } from "./sse.js";
import { enableRipple, initTooltips, toast } from "./ui.js";
import { $, $$, hideCookieBanner } from "./util.js";

const TABS = ["new", "queue", "library"];
const desktop = matchMedia("(min-width: 992px)");
let tab = "queue";
let completedSeen = null;

function applyTab() {
  // desktop: composer is always visible, "new" just means "queue + focus the box"
  const eff = desktop.matches && tab === "new" ? "queue" : tab;
  document.body.dataset.tab = eff;
  $("#panel-new").hidden = !desktop.matches && eff !== "new";
  $("#panel-queue").hidden = eff !== "queue";
  $("#panel-library").hidden = eff !== "library";
  $$("#tabs-top button, #bottom-nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === eff));
  if (eff === "library") loadLibrary(currentPath());
}

function setTab(next, { focus = false } = {}) {
  if (!TABS.includes(next)) next = "queue";
  tab = next;
  applyTab();
  history.replaceState(null, "", "#" + next);
  if (focus || next === "new") window.scrollTo({ top: 0, behavior: "smooth" });
}

/** Top-bar badge: which mode this server runs in, and (on hover) what that means. */
function paintMode(cfg) {
  const server = !!cfg.server_mode;
  const chip = $("#mode-chip");
  chip.classList.toggle("server", server);
  chip.querySelector("i").className = `bi bi-${server ? "globe2" : "laptop"}`;
  $("#mode-text").textContent = server ? "Server mode" : "Local mode";
  const ttl = cfg.delivery_ttl_minutes || 60;
  const tip = server
    ? `<b>Server mode</b> — running as a shared site.<ul class="mode-tip">
        <li>Your downloads are private to this browser</li>
        <li>Finished files: use <b>Save</b> on the job card (deleted from the server after ${ttl} min)</li>
        <li>Library = shared host files (read-only), not your personal downloads</li>
        <li>Use your own YouTube cookies (Settings)</li></ul>
       <b>Local mode</b> — a personal downloader: one shared queue, files stay in the server's downloads folder, everything is open.`
    : `<b>Local mode</b> — a personal downloader.<ul class="mode-tip">
        <li>One queue for everyone using this app</li>
        <li>Files stay in the downloads folder and the library</li>
        <li>Everything is open: library edits, limits, cookies</li></ul>
       <b>Server mode</b> (<code>SERVER_MODE=true</code>) — for a public site: private queue per browser, files handed to the browser then deleted after ${ttl} min, admin-locked library, per-visitor cookies.`;
  chip.setAttribute("data-bs-original-title", tip);
  bootstrap.Tooltip.getInstance(chip)?.setContent({ ".tooltip-inner": tip });
  const libTip = $("#lib-mode-tip");
  if (libTip) {
    libTip.hidden = !server;
    libTip.innerHTML = server
      ? `<i class="bi bi-info-circle"></i> Library shows <b>shared host files</b>. Your finished downloads appear as <b>Save</b> on the Downloads job card (kept ~${ttl} min).`
      : "";
  }
}

/** Server mode: tell visitors up front that new downloads are refused (the server enforces it: HTTP 503). */
function paintMaintenance(cfg) {
  const on = !!(cfg?.server_mode && cfg.maintenance);
  $("#maint-banner").hidden = !on;
  $("#maint-banner-text").textContent = on ? (cfg.maintenance_message || "Please try again later.") : "";
}

async function pollMaintenance() {
  if (!document.body.classList.contains("server-mode")) return;
  try { paintMaintenance(await api.config()); } catch { /* the health pill already says the server is away */ }
}

async function pollHealth() {
  try { renderHealth(await api.health()); } catch { renderHealth(null); }
}

function setLive(state) {
  const dot = $("#live-dot");
  const live = state === "live";
  dot.classList.toggle("off", !live);
  $("#live-text").textContent = live ? "live" : "reconnecting";
  $("#live-chip").setAttribute("data-bs-original-title", live ? "Live updates connected" : "Connection lost — retrying");
}

// ── resizable columns (desktop): drag the divider, ←/→ to nudge, double-click / Home to reset ──
const SPLIT_KEY = "mst.leftw", SPLIT_DEFAULT = 440, SPLIT_MIN = 340, SPLIT_RIGHT_MIN = 460;

function initSplitter() {
  const layout = $(".layout"), bar = $("#splitter");
  if (!layout || !bar) return;
  const limit = (w) => Math.round(Math.max(SPLIT_MIN, Math.min(w, layout.clientWidth - SPLIT_RIGHT_MIN - 20)));
  const apply = (w) => { layout.style.setProperty("--left-w", limit(w) + "px"); bar.setAttribute("aria-valuenow", String(limit(w))); };
  const save = () => { try { localStorage.setItem(SPLIT_KEY, String(parseInt(layout.style.getPropertyValue("--left-w")))); } catch { /* private mode */ } };
  let saved = SPLIT_DEFAULT;
  try { saved = Number(localStorage.getItem(SPLIT_KEY)) || SPLIT_DEFAULT; } catch { /* private mode */ }
  apply(saved);
  window.addEventListener("resize", () => apply(parseInt(layout.style.getPropertyValue("--left-w")) || SPLIT_DEFAULT));

  bar.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    bar.setPointerCapture(e.pointerId);
    layout.classList.add("is-resizing"); document.body.classList.add("is-resizing");
    const left = layout.getBoundingClientRect().left;
    const move = (ev) => apply(ev.clientX - left - 10);   // 10 = half the gap, so the bar follows the pointer
    const stop = () => {
      bar.removeEventListener("pointermove", move); bar.removeEventListener("pointerup", stop); bar.removeEventListener("pointercancel", stop);
      layout.classList.remove("is-resizing"); document.body.classList.remove("is-resizing"); save();
    };
    bar.addEventListener("pointermove", move); bar.addEventListener("pointerup", stop); bar.addEventListener("pointercancel", stop);
  });
  bar.addEventListener("dblclick", () => { apply(SPLIT_DEFAULT); save(); });
  bar.addEventListener("keydown", (e) => {
    const cur = parseInt(layout.style.getPropertyValue("--left-w")) || SPLIT_DEFAULT;
    const step = e.shiftKey ? 80 : 20;
    if (e.key === "ArrowLeft") apply(cur - step);
    else if (e.key === "ArrowRight") apply(cur + step);
    else if (e.key === "Home") apply(SPLIT_DEFAULT);
    else return;
    e.preventDefault(); save();
  });
}

async function boot() {
  enableRipple();
  initTooltips();
  initQueue();
  initLibrary();
  initDeliveries();
  initSettings();
  initConnect();
  initPlayer();
  initStage();
  initSplitter();
  $("#cookie-banner-fix")?.addEventListener("click", () => window.dispatchEvent(new CustomEvent("open-cookies")));
  $("#cookie-banner-dismiss")?.addEventListener("click", hideCookieBanner);
  window.addEventListener("cookies-changed", hideCookieBanner);

  // tabs
  $("#tabs-top").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) setTab(b.dataset.tab); });
  $("#bottom-nav").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) setTab(b.dataset.tab); });
  desktop.addEventListener("change", applyTab);
  window.addEventListener("go-queue", () => setTab("queue"));
  window.addEventListener("go-new", () => setTab("new"));
  window.addEventListener("open-folder", (e) => { tab = "library"; applyTab(); history.replaceState(null, "", "#library"); loadLibrary(e.detail || ""); });
  const initial = location.hash.slice(1);
  tab = TABS.includes(initial) ? initial : "queue";
  applyTab();

  // config + runtime
  try {
    const [cfg, rt] = await Promise.all([api.config(), api.runtime()]);
    document.body.classList.toggle("server-mode", !!cfg.server_mode);  // before anything renders
    paintMode(cfg);
    paintMaintenance(cfg);
    setQueueMode(cfg);
    initComposer(cfg);
    initCart(cfg);
    initSubscriptions(cfg);
    setParCap(cfg.max_parallel_cap);
    showRuntime(rt);
    $("#s-par").max = cfg.max_parallel_cap;
  } catch (e) {
    toast(e.detail || "Could not load configuration", "err", { title: "Backend unreachable" });
  }

  // live queue
  connectEvents({
    onStatus: setLive,
    onSnapshot: (snap) => {
      applySnapshot(snap);
      const done = snap.stats?.completed;
      if (done != null) {
        if (completedSeen != null && done > completedSeen) {
          if (tab === "library") loadLibrary(currentPath());
          refreshFolders();  // a finished download may have created a new folder
        }
        completedSeen = done;
      }
    },
  });

  pollHealth();
  setInterval(pollHealth, 15000);
  setInterval(pollMaintenance, 60000);  // the admin can flip it live
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollHealth(); pollMaintenance(); } });
}

boot();
