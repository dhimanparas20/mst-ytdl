// Browser-local UI preferences (localStorage ``mst.ui``). Device-specific — never Redis.

import { confirmDialog, toast } from "./ui.js";
import { $, $$, store } from "./util.js";

const KEY = "mst.ui";

/** Lyric language picker (mirrors ``services.lyrics.LYRIC_LANGS``). */
export const LYRIC_LANG_OPTIONS = [
  ["auto", "Auto (original)"],
  ["en", "English"], ["es", "Spanish"], ["fr", "French"], ["de", "German"],
  ["pt", "Portuguese"], ["it", "Italian"], ["ja", "Japanese"], ["ko", "Korean"],
  ["zh-CN", "Chinese (Simplified)"], ["zh-TW", "Chinese (Traditional)"],
  ["hi", "Hindi"], ["ar", "Arabic"], ["ru", "Russian"], ["th", "Thai"],
  ["vi", "Vietnamese"], ["id", "Indonesian"], ["tr", "Turkish"],
];

/** How search / explore / Mine feed rows are arranged (omnibar stays a compact list). */
export const RESULTS_LAYOUT_OPTIONS = [
  ["list", "List", "list-ul"],
  ["cards", "Cards", "grid"],
  ["posters", "Posters", "images"],
  ["stack", "Stack", "layers"],
  ["cinema", "Cinema", "film"],
  ["mosaic", "Mosaic", "columns-gap"],
];

const RESULTS_LAYOUTS = new Set(RESULTS_LAYOUT_OPTIONS.map(([c]) => c));

const DEFAULTS = {
  reduceMotion: false,     // calmer animations
  compactQueue: false,     // denser download cards
  autoSave: true,          // server mode: auto-download finished jobs this tab saw finish
  confirmDelete: true,     // confirm before removing jobs / library files
  exploreDefault: "auto",  // auto | music | videos | for_you
  omnibarMusic: false,     // prefer YouTube Music links in the top search
  openQueueOnSubmit: true, // jump to /download after queueing
  lyricsLang: "auto",      // auto = original LRCLIB text; else translate / prefer that language
  resultsLayout: "cards",  // list | cards | posters | stack | cinema | mosaic
  ambientViz: false,       // reactive page background while the mini player is playing
  exploreAnon: false,      // experimental: show Explore Music/Videos without login cookies
};

let ui = { ...DEFAULTS };

export function getUiPrefs() {
  return { ...ui };
}

export function loadUiPrefs() {
  const saved = store(KEY);
  ui = { ...DEFAULTS, ...(saved && typeof saved === "object" ? saved : {}) };
  if (!["auto", "music", "videos", "for_you"].includes(ui.exploreDefault)) ui.exploreDefault = "auto";
  const langs = new Set(LYRIC_LANG_OPTIONS.map(([c]) => c));
  if (!langs.has(ui.lyricsLang)) ui.lyricsLang = "auto";
  if (!RESULTS_LAYOUTS.has(ui.resultsLayout)) ui.resultsLayout = "list";
  applyUiPrefs();
  return getUiPrefs();
}

export function setUiPrefs(patch) {
  const next = { ...ui, ...patch };
  if (!RESULTS_LAYOUTS.has(next.resultsLayout)) next.resultsLayout = "list";
  // Skip store + event when nothing actually changed (avoids search ↔ prefs feedback loops).
  const keys = new Set([...Object.keys(ui), ...Object.keys(next)]);
  let changed = false;
  for (const k of keys) {
    if (ui[k] !== next[k]) { changed = true; break; }
  }
  if (!changed) return getUiPrefs();
  ui = next;
  store(KEY, ui);
  applyUiPrefs();
  window.dispatchEvent(new CustomEvent("ui-prefs", { detail: getUiPrefs() }));
  return getUiPrefs();
}

export function resetUiPrefs() {
  try { localStorage.removeItem(KEY); } catch { /* */ }
  ui = { ...DEFAULTS };
  applyUiPrefs();
  window.dispatchEvent(new CustomEvent("ui-prefs", { detail: getUiPrefs() }));
  return getUiPrefs();
}

/** Read / write a themed Music icon toggle (``aria-pressed``). */
export function musicTogOn(el) {
  return !!el && (el.getAttribute("aria-pressed") === "true" || el.classList.contains("on"));
}

export function setMusicTog(el, on) {
  if (!el) return;
  const v = !!on;
  el.setAttribute("aria-pressed", String(v));
  el.classList.toggle("on", v);
}

/** Apply body classes / one-shot chrome from the current prefs. */
export function applyUiPrefs() {
  document.body.classList.toggle("reduce-motion", !!ui.reduceMotion);
  document.body.classList.toggle("compact-queue", !!ui.compactQueue);
  for (const [code] of RESULTS_LAYOUT_OPTIONS) {
    document.body.classList.toggle(`rl-${code}`, ui.resultsLayout === code);
  }
  setMusicTog($("#pref-omnibar-music"), ui.omnibarMusic);
  paintResultsLayoutSegs();
}

/** Paint every ``[data-results-layout]`` seg to match the current pref. */
export function paintResultsLayoutSegs() {
  $$("[data-results-layout]").forEach((b) => {
    b.classList.toggle("active", b.dataset.resultsLayout === ui.resultsLayout);
  });
  const sel = $("#pref-results-layout");
  if (sel) sel.value = ui.resultsLayout || "list";
}

/** Markup for an inline layout switcher (explore / search). */
export function resultsLayoutSegHTML() {
  return `<div class="seg results-layout-seg" role="group" aria-label="Results layout">
    ${RESULTS_LAYOUT_OPTIONS.map(([code, label, icon]) =>
      `<button type="button" data-results-layout="${code}" title="${label}" aria-label="${label}" class="${ui.resultsLayout === code ? "active" : ""}"><i class="bi bi-${icon}"></i><span class="d-none d-xl-inline">${label}</span></button>`
    ).join("")}
  </div>`;
}

/** Paint the Settings form controls from prefs. */
export function paintUiPrefsForm() {
  const map = {
    "pref-motion": "reduceMotion",
    "pref-compact": "compactQueue",
    "pref-autosave": "autoSave",
    "pref-confirm": "confirmDelete",
    "pref-queue-jump": "openQueueOnSubmit",
    "pref-ambient": "ambientViz",
    "pref-explore-anon": "exploreAnon",
  };
  Object.entries(map).forEach(([id, key]) => {
    const el = $(`#${id}`);
    if (el) el.checked = !!ui[key];
  });
  setMusicTog($("#pref-omnibar-music"), ui.omnibarMusic);
  const ex = $("#pref-explore");
  if (ex) ex.value = ui.exploreDefault;
  const ly = $("#pref-lyrics-lang");
  if (ly) {
    if (!ly.options.length) {
      ly.innerHTML = LYRIC_LANG_OPTIONS.map(([c, label]) => `<option value="${c}">${label}</option>`).join("");
    }
    ly.value = ui.lyricsLang || "auto";
  }
  const rl = $("#pref-results-layout");
  if (rl) {
    if (!rl.options.length) {
      rl.innerHTML = RESULTS_LAYOUT_OPTIONS.map(([c, label]) => `<option value="${c}">${label}</option>`).join("");
    }
    rl.value = ui.resultsLayout || "list";
  }
}

/** Read the Settings form into prefs (instant save — no Apply needed). */
export function readUiPrefsForm() {
  return setUiPrefs({
    reduceMotion: !!$("#pref-motion")?.checked,
    compactQueue: !!$("#pref-compact")?.checked,
    autoSave: !!$("#pref-autosave")?.checked,
    confirmDelete: !!$("#pref-confirm")?.checked,
    openQueueOnSubmit: !!$("#pref-queue-jump")?.checked,
    ambientViz: !!$("#pref-ambient")?.checked,
    exploreAnon: !!$("#pref-explore-anon")?.checked,
    omnibarMusic: musicTogOn($("#pref-omnibar-music")),
    exploreDefault: $("#pref-explore")?.value || "auto",
    lyricsLang: $("#pref-lyrics-lang")?.value || "auto",
    resultsLayout: $("#pref-results-layout")?.value || "list",
  });
}

export function initUiPrefs() {
  loadUiPrefs();
  // Inline layout chips (explore / search / Mine).
  const seg = resultsLayoutSegHTML();
  $$(".results-layout-host").forEach((host) => { host.innerHTML = seg; });
  paintResultsLayoutSegs();
  const form = $("#ui-prefs");
  if (form) {
    form.addEventListener("change", async (e) => {
      // Experimental anon Explore: warn before enabling (may 429 / fail without cookies).
      if (e.target?.id === "pref-explore-anon" && e.target.checked && !ui.exploreAnon) {
        const ok = await confirmDialog({
          title: "Enable experimental Explore?",
          body: "Anonymous Trending music / videos is in beta. It often works, but can fail, return empty results, or hit rate limits when YouTube blocks cookie-less searches. Prefer adding cookies for a stable shelf.",
          confirmText: "Enable anyway",
          icon: "bi-flask",
        });
        if (!ok) {
          e.target.checked = false;
          return;
        }
      }
      readUiPrefsForm();
    });
    $("#pref-reset")?.addEventListener("click", () => {
      resetUiPrefs();
      paintUiPrefsForm();
      toast("Preferences reset for this browser.", "ok", { ms: 2200 });
    });
  }
  // Look tab (outside ``#ui-prefs``): ambient visual lives with the theme picker.
  $("#pref-ambient")?.addEventListener("change", () => {
    setUiPrefs({ ambientViz: !!$("#pref-ambient").checked });
  });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-results-layout]");
    if (b) {
      setUiPrefs({ resultsLayout: b.dataset.resultsLayout || "list" });
      return;
    }
    const music = e.target.closest("#pref-omnibar-music");
    if (!music) return;
    const next = !musicTogOn(music);
    setMusicTog(music, next);
    setUiPrefs({ omnibarMusic: next });
  });
}
