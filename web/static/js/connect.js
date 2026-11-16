// "Sign in with Google": opens the Google sign-in in a new tab, shows a one-time code, and waits for the
// browser extension (extension/) to upload this browser's YouTube cookies. See services/connect.py.

import { api } from "./api.js";
import { toast, withBusy } from "./ui.js";
import { $, $$ } from "./util.js";

// After Google sign-in, land on robots.txt (not the YouTube homepage) so the session is quieter and easier to keep from rotating.
const YT_ROBOTS = "https://www.youtube.com/robots.txt";
const GOOGLE_SIGN_IN = "https://accounts.google.com/ServiceLogin?service=youtube&continue=" + encodeURIComponent(YT_ROBOTS);
const POLL_MS = 2000;
// phones and tablets cannot run the extension: they hand the code to a computer instead (the code works from any device)
const isPhone = () => window.matchMedia("(pointer: coarse) and (max-width: 1024px)").matches;
let timer = null;
let cfg = null;

const say = (html, cls = "") => { const el = $("#cn-status"); el.className = `small mt-2 ${cls}`; el.innerHTML = html; };

function stop() { clearTimeout(timer); timer = null; }

function setStep(n) {
  const step = Math.max(1, Math.min(4, Number(n) || 1));
  $$("#cn-stepper .cn-step").forEach((el) => el.classList.toggle("active", Number(el.dataset.step) === step));
  $$("#cn-steps .cn-pane").forEach((el) => {
    const pane = Number(el.dataset.pane);
    // phone: only the code pane; desktop: active step pane
    el.hidden = isPhone() ? pane !== 3 : pane !== step;
  });
}

function reset() {
  stop();
  $("#cn-steps").hidden = true;
  $("#cn-go").hidden = false;
  setStep(1);
  say("");
}

function poll(code, until) {
  timer = setTimeout(async () => {
    if (Date.now() > until) { reset(); return say("The code expired — press Sign in again.", "text-warning"); }
    try {
      const st = await api.connectStatus(code);
      if (st.state === "done") {
        reset();
        say(`<i class="bi bi-check-circle-fill" style="color:var(--ok)"></i> Connected — your YouTube account is linked.`);
        toast("YouTube account connected.", "ok", { ms: 3500 });
        window.dispatchEvent(new CustomEvent("cookies-changed"));
        return;
      }
    } catch (e) {
      if (e.status === 404) { reset(); return say("The code expired — press Sign in again.", "text-warning"); }
      // a hiccup (network, rate limit): keep waiting
    }
    poll(code, until);
  }, POLL_MS);
}

async function start(btn) {
  if (!isPhone()) window.open(GOOGLE_SIGN_IN, "_blank", "noopener");  // first, synchronously: it must stay inside the click or pop-up blockers stop it
  try {
    const r = await withBusy(btn, () => api.connectStart());
    $("#cn-code").textContent = r.code;
    $("#cn-server").textContent = r.server;
    $("#cn-steps").hidden = false;
    btn.hidden = true;
    setStep(isPhone() ? 3 : 1);
    say(`<span class="spinner-border spinner-border-sm me-1"></span> Waiting for the extension…`);
    poll(r.code, Date.now() + r.expires_in * 1000);
  } catch (e) {
    say(e.detail || e.message, "text-danger");
  }
}

export function initConnect() {
  const modal = $("#cookies-modal");
  if (!modal) return;
  modal.addEventListener("show.bs.modal", async () => {
    reset();
    try { cfg = await api.config(); } catch { /* keep the box hidden */ }
    $("#connect-box").hidden = $("#cn-or").hidden = !cfg?.connect_enabled;
    $("#connect-box").classList.toggle("is-phone", isPhone());
    $("#cn-hours").textContent = cfg?.server_mode ? String(cfg.visitor_cookie_hours || 24) : "";
    $("#cn-keep").hidden = !cfg?.server_mode;
  });
  modal.addEventListener("hidden.bs.modal", stop);
  $("#cn-go").addEventListener("click", (e) => start(e.currentTarget));
  const open = $("#cn-open");
  if (open) open.href = GOOGLE_SIGN_IN;
  $("#cn-steps")?.addEventListener("click", (e) => {
    const next = e.target.closest("[data-cn-next]");
    if (next) setStep(next.dataset.cnNext);
    const step = e.target.closest("#cn-stepper [data-step]");
    if (step) setStep(step.dataset.step);
  });
  $("#cn-copy")?.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#cn-code").textContent); toast("Code copied.", "ok", { ms: 1500 }); } catch { /* clipboard blocked */ }
  });
}
