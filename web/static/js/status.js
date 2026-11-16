// Single top-bar status button: mode · live · cookies · health in one menu.
// Replaces the four always-visible chips so the omnibar has room.

import { $, esc } from "./util.js";

const S = {
  modeServer: false,
  modeTip: "Mode",
  live: false,
  liveText: "connecting",
  cookie: { hidden: true, tone: "none", text: "Cookies", title: "" },
  health: { cls: "ok", text: "Checking…", tip: "Checking…" },
};

let open = false;

function toneRank(t) {
  return { bad: 3, warn: 2, ok: 1, none: 0, off: 2 }[t] || 0;
}

function worstTone() {
  const parts = [
    S.health.cls,
    S.live ? "ok" : "warn",
    S.cookie.hidden ? "ok" : (S.cookie.tone === "none" ? "warn" : S.cookie.tone),
  ];
  return parts.sort((a, b) => toneRank(b) - toneRank(a))[0] || "ok";
}

function paintBtn() {
  const btn = $("#status-btn");
  if (!btn) return;
  const tone = worstTone();
  btn.className = `btn-icon status-btn ${tone}`;
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  const tip = S.health.text;
  btn.setAttribute("title", tip);
  btn.setAttribute("aria-label", `Status: ${tip}`);
  const dot = btn.querySelector(".status-dot");
  if (dot) dot.className = `status-dot ${tone}`;
}

function row(icon, label, value, { tone = "", action = "", btn = "" } = {}) {
  const act = action
    ? `<button type="button" class="btn-ghost sm" data-status-act="${action}">${btn || "Open"}</button>`
    : "";
  return `<div class="status-row${tone ? ` ${tone}` : ""}">
    <i class="bi bi-${icon}"></i>
    <div class="status-main"><div class="status-k">${esc(label)}</div><div class="status-v">${esc(value)}</div></div>
    ${act}
  </div>`;
}

function renderMenu() {
  const menu = $("#status-menu");
  if (!menu) return;
  const modeIcon = S.modeServer ? "globe2" : "laptop";
  const modeLabel = S.modeServer ? "Server mode" : "Local mode";
  const liveLabel = S.live ? "Live updates connected" : "Reconnecting…";
  const ck = S.cookie;
  const cookieRow = ck.hidden
    ? ""
    : row("key-fill", "Cookies", ck.text, { tone: ck.tone, action: "cookies", btn: "Edit" });
  menu.innerHTML = `
    <div class="status-head">System status</div>
    ${row(modeIcon, "Mode", modeLabel)}
    ${row(S.live ? "broadcast" : "wifi-off", "Live", liveLabel, { tone: S.live ? "ok" : "warn" })}
    ${cookieRow}
    ${row("heart-pulse", "Health", S.health.text, { tone: S.health.cls, action: "settings", btn: "Details" })}
    <div class="status-tip faint small">${esc(S.health.tip)}</div>
  `;
  const first = menu.querySelector(".status-row");
  if (first) first.title = S.modeTip.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function setOpen(v) {
  open = v;
  const menu = $("#status-menu");
  if (menu) menu.hidden = !v;
  paintBtn();
}

export function paintMode(cfg) {
  S.modeServer = !!cfg.server_mode;
  const ttl = cfg.delivery_ttl_minutes || 60;
  S.modeTip = S.modeServer
    ? `Server mode — private queue per browser; files expire after ${ttl} min`
    : "Local mode — personal downloader; files stay in the downloads folder";
  // keep body class / library tips owned by app.js; here we only paint the menu
  renderMenu();
  paintBtn();
}

export function setLive(state) {
  S.live = state === "live";
  S.liveText = S.live ? "live" : "reconnecting";
  renderMenu();
  paintBtn();
}

export function paintCookieStatus(state) {
  // state null → hide cookies row; else { tone, text, title }
  if (!state) {
    S.cookie = { hidden: true, tone: "none", text: "Cookies", title: "" };
  } else {
    S.cookie = { hidden: false, tone: state.tone || "none", text: state.text, title: state.title || state.text };
  }
  renderMenu();
  paintBtn();
}

export function paintHealthStatus(h) {
  let cls = "ok", text = "All systems go";
  if (!h) { cls = "bad"; text = "Server offline"; }
  else if (!h.redis) { cls = "bad"; text = "Redis down"; }
  else if (!h.worker) { cls = "warn"; text = "No worker"; }
  else if (!h.ffmpeg) { cls = "warn"; text = "ffmpeg missing"; }
  else if (!h.js_runtime) { cls = "warn"; text = "No JS runtime"; }
  const tip = h
    ? (h.detail?.length ? h.detail.join(" • ") : `yt-dlp ${h.yt_dlp} · ffmpeg ✓ · worker ✓ · ${h.js_runtime || "no JS"}`)
    : "Cannot reach the API";
  S.health = { cls, text, tip };
  renderMenu();
  paintBtn();
  return { cls, text, tip };
}

export function initStatus() {
  const btn = $("#status-btn");
  const wrap = $("#status-wrap");
  if (!btn || !wrap) return;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(!open);
  });
  $("#status-menu")?.addEventListener("click", (e) => {
    const a = e.target.closest("[data-status-act]");
    if (!a) return;
    setOpen(false);
    if (a.dataset.statusAct === "cookies") window.dispatchEvent(new CustomEvent("open-cookies"));
    if (a.dataset.statusAct === "settings") window.dispatchEvent(new CustomEvent("open-settings"));
  });
  document.addEventListener("pointerdown", (e) => {
    if (open && !wrap.contains(e.target)) setOpen(false);
  });
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && open) setOpen(false); });
  renderMenu();
  paintBtn();
}
