// Cross-cutting UI helpers: toasts, confirm dialog, tooltips, ripple.

import { $, esc, htmlToEl } from "./util.js";

const ICONS = { ok: "check-circle-fill", err: "x-octagon-fill", warn: "exclamation-triangle-fill", info: "info-circle-fill" };
const COLORS = { ok: "var(--ok)", err: "var(--bad)", warn: "var(--warn)", info: "var(--g2)" };

/** Show a toast. kind: ok | err | warn | info */
export function toast(message, kind = "info", { title, ms = 5200 } = {}) {
  const host = $("#toasts");
  const el = htmlToEl(`
    <div class="toast t-${kind}" role="${kind === "err" ? "alert" : "status"}" aria-live="${kind === "err" ? "assertive" : "polite"}" aria-atomic="true">
      <div class="d-flex align-items-start gap-3 p-3">
        <i class="bi bi-${ICONS[kind] || ICONS.info}" style="color:${COLORS[kind] || COLORS.info};font-size:19px"></i>
        <div class="flex-grow-1" style="min-width:0">
          ${title ? `<div class="fw-semibold">${esc(title)}</div>` : ""}
          <div class="${title ? "muted" : ""}" style="font-size:13.5px;word-break:break-word">${esc(message)}</div>
        </div>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="toast" aria-label="Close"></button>
      </div>
      <div class="t-bar" style="--dur:${ms}ms"></div>
    </div>`);
  host.appendChild(el);
  const t = new bootstrap.Toast(el, { delay: ms });
  el.addEventListener("hidden.bs.toast", () => el.remove());
  t.show();
  // keep the stack short
  while (host.children.length > 5) host.firstElementChild.remove();
}

/** Promise-based confirm dialog. */
export function confirmDialog({ title = "Are you sure?", body = "", confirmText = "Confirm", danger = false, icon } = {}) {
  return new Promise((resolve) => {
    const modalEl = $("#confirm-modal");
    $("#confirm-title").textContent = title;
    $("#confirm-body").textContent = body;
    const yes = $("#confirm-yes");
    yes.innerHTML = esc(confirmText);
    yes.style.setProperty("--glow", danger ? "rgba(251,113,133,.5)" : "rgba(106, 88, 214,.45)");
    yes.style.background = danger ? "linear-gradient(135deg,#fb7185,#e11d48)" : "";
    yes.style.color = danger ? "#fff" : "";
    $("#confirm-ico").innerHTML = `<i class="bi bi-${icon || (danger ? "exclamation-triangle-fill" : "question-circle")}" style="color:${danger ? "var(--bad)" : "var(--g2)"}"></i>`;
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    let answer = false;
    // Bootstrap ignores hide() while the fade-in is still running; retry once it has finished
    // so a quick click on "Confirm" is never swallowed.
    const onYes = () => { answer = true; modal.hide(); };
    const onShown = () => { if (answer) modal.hide(); };
    const onHidden = () => {
      yes.removeEventListener("click", onYes);
      modalEl.removeEventListener("shown.bs.modal", onShown);
      modalEl.removeEventListener("hidden.bs.modal", onHidden);
      resolve(answer);
    };
    yes.addEventListener("click", onYes);
    modalEl.addEventListener("shown.bs.modal", onShown);
    modalEl.addEventListener("hidden.bs.modal", onHidden);
    modal.show();
  });
}

/**
 * Ask for one line of text (rename, move-to-folder…). Resolves to the trimmed string,
 * or ``null`` when cancelled. ``suggestions`` feeds a datalist (e.g. existing folders).
 */
export function promptDialog({ title, body = "", value = "", confirmText = "OK", suggestions = [] } = {}) {
  return new Promise((resolve) => {
    const modalEl = $("#prompt-modal");
    $("#prompt-title").textContent = title;
    $("#prompt-body").textContent = body;
    $("#prompt-body").hidden = !body;
    const input = $("#prompt-input");
    input.value = value;
    $("#prompt-list").innerHTML = suggestions.map((s) => `<option value="${esc(s)}"></option>`).join("");
    $("#prompt-ok").textContent = confirmText;
    const form = $("#prompt-form");
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    let answer = null;
    const onSubmit = (e) => { e.preventDefault(); answer = input.value.trim(); modal.hide(); };
    const onShown = () => {
      if (answer !== null) { modal.hide(); return; }  // submitted during the fade-in: hide() was ignored
      input.focus(); input.select();
    };
    const onHidden = () => {
      form.removeEventListener("submit", onSubmit);
      modalEl.removeEventListener("shown.bs.modal", onShown);
      modalEl.removeEventListener("hidden.bs.modal", onHidden);
      resolve(answer);
    };
    form.addEventListener("submit", onSubmit);
    modalEl.addEventListener("shown.bs.modal", onShown);
    modalEl.addEventListener("hidden.bs.modal", onHidden);
    modal.show();
  });
}

/** (Re)initialise Bootstrap tooltips inside root. */
export function initTooltips(root = document) {
  root.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
    if (!bootstrap.Tooltip.getInstance(el)) new bootstrap.Tooltip(el, { trigger: "hover focus", container: "body" });
  });
}

/** Material-style ripple on gradient buttons. */
export function enableRipple() {
  document.addEventListener("pointerdown", (e) => {
    const b = e.target.closest(".btn-grad");
    if (!b || b.disabled) return;
    const r = b.getBoundingClientRect();
    const size = Math.max(r.width, r.height);
    const s = document.createElement("span");
    s.className = "ripple";
    s.style.cssText = `width:${size}px;height:${size}px;left:${e.clientX - r.left - size / 2}px;top:${e.clientY - r.top - size / 2}px`;
    b.appendChild(s);
    setTimeout(() => s.remove(), 700);
  });
}

/** Run an async action on a button with a spinner; always restores it. */
export async function withBusy(btn, fn) {
  if (btn) { btn.disabled = true; btn.classList.add("busy"); }
  try { return await fn(); }
  finally { if (btn) { btn.disabled = false; btn.classList.remove("busy"); } }
}

export function copyText(text) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  const ta = document.createElement("textarea");
  ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
  return Promise.resolve();
}
