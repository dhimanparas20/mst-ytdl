// Bootstrap: wires tabs, live updates, health polling and the feature modules.

import { api } from "./api.js";
import { initComposer, refreshFolders } from "./composer.js";
import { initPlayer } from "./player.js";
import { initLibrary, loadLibrary, currentPath } from "./library.js";
import { applySnapshot, initQueue, setParCap, showRuntime } from "./queue.js";
import { initSettings, renderHealth } from "./settings.js";
import { connectEvents } from "./sse.js";
import { enableRipple, initTooltips, toast } from "./ui.js";
import { $, $$ } from "./util.js";

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

async function boot() {
  enableRipple();
  initTooltips();
  initQueue();
  initLibrary();
  initSettings();
  initPlayer();

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
    initComposer(cfg);
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
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollHealth(); });
}

boot();
