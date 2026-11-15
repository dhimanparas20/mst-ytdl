// Server mode: the admin unlock. Library edits, download limits and YouTube cookies are
// hidden (CSS: body.server-mode:not(.is-admin) .admin-ctl) until the admin password is entered.
// Personal mode: the server reports admin=true, so everything stays visible and open.

import { api } from "./api.js";
import { toast, withBusy } from "./ui.js";
import { $, esc } from "./util.js";

let state = { server_mode: false, enabled: false, admin: true };

export const adminState = () => state;

function setTip(el, tip) {
  el.setAttribute("title", tip);
  el.setAttribute("data-bs-original-title", tip);
  bootstrap.Tooltip.getInstance(el)?.setContent({ ".tooltip-inner": tip });
}

function paint() {
  document.body.classList.toggle("server-mode", !!state.server_mode);
  document.body.classList.toggle("is-admin", !!state.admin);
  const chip = $("#admin-chip");
  const on = state.server_mode && state.admin;
  chip.classList.toggle("on", on);
  chip.innerHTML = `<i class="bi bi-${on ? "unlock-fill" : "lock-fill"}"></i><span class="d-none d-sm-inline">${on ? "Admin" : "Read-only"}</span>`;
  setTip(chip, on ? "Signed in as admin — click to lock" : state.enabled ? "Admin: unlock library management" : "Admin access is not configured on this server");

  // settings modal section
  $("#adm-status").innerHTML = on
    ? `<i class="bi bi-unlock-fill" style="color:var(--ok)"></i> Admin unlocked`
    : `<i class="bi bi-lock-fill" style="color:var(--warn)"></i> Public server — your queue is private`;
  $("#adm-detail").textContent = on
    ? "You can manage the library, download limits and YouTube cookies."
    : state.enabled ? "Finished downloads are handed to your browser and removed from the server afterwards." : "No admin password is configured (SERVER_ADMIN_PASSWORD_HASH).";
  const btn = $("#adm-toggle");
  btn.hidden = !state.enabled;
  btn.innerHTML = on ? `<i class="bi bi-lock"></i>Lock` : `<i class="bi bi-unlock"></i>Unlock`;
  window.dispatchEvent(new CustomEvent("admin-change", { detail: state }));
}

/** Ask the server who we are (call once at boot, and after login/logout). */
export async function refreshAdmin() {
  try { state = await api.admin(); } catch { /* keep the last known state */ }
  paint();
  return state;
}

export function openAdminLogin() {
  if (!state.enabled) { toast("No admin password is configured on this server (SERVER_ADMIN_PASSWORD_HASH in .env).", "warn"); return; }
  bootstrap.Modal.getInstance($("#settings-modal"))?.hide();  // one modal at a time
  $("#admin-pw").value = "";
  $("#admin-err").textContent = "";
  bootstrap.Modal.getOrCreateInstance($("#admin-modal")).show();
}

async function login(e) {
  e.preventDefault();
  const pw = $("#admin-pw").value;
  if (!pw) return;
  try {
    state = await withBusy($("#admin-ok"), () => api.adminLogin(pw));
    $("#admin-pw").value = "";
    bootstrap.Modal.getInstance($("#admin-modal"))?.hide();
    paint();
    toast("Admin unlocked — library management is available.", "ok", { ms: 3000 });
  } catch (err) {
    $("#admin-err").innerHTML = esc(err.detail || err.message);
    $("#admin-pw").select();
  }
}

export async function lockAdmin() {
  try { state = await api.adminLogout(); } catch { /* the cookie is gone either way on the next check */ }
  paint();
  toast("Admin locked.", "info", { ms: 2200 });
}

const toggle = () => (state.server_mode && state.admin ? lockAdmin() : openAdminLogin());

export function initAdmin() {
  $("#admin-form").addEventListener("submit", login);
  $("#admin-chip").addEventListener("click", toggle);
  $("#adm-toggle").addEventListener("click", toggle);
  $("#lib-unlock").addEventListener("click", (e) => { e.preventDefault(); openAdminLogin(); });
  $("#admin-modal").addEventListener("shown.bs.modal", () => $("#admin-pw").focus());
}
