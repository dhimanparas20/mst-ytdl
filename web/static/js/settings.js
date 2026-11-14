// Settings modal (runtime limits + system info) and the header health pill.

import { api } from "./api.js";
import { toast, withBusy } from "./ui.js";
import { $, esc, setText } from "./util.js";
import { showRuntime } from "./queue.js";

let cfg = null;

function shortPath(p, n = 46) {
  if (!p) return "—";
  return p.length > n ? "…" + p.slice(-(n - 1)) : p;
}

const row = (k, v, ok) =>
  `<div class="flex justify-between gap-3 py-1" style="border-bottom:1px solid var(--stroke)"><span class="muted">${k}</span><span class="mono text-end" style="word-break:break-all">${ok === undefined ? "" : `<i class="bi bi-${ok ? "check-circle-fill" : "x-circle-fill"}" style="color:var(--${ok ? "ok" : "bad"})"></i> `}${v}</span></div>`;

async function fillSystem() {
  const [h, c] = await Promise.all([api.health(), cfg ? Promise.resolve(cfg) : api.config()]);
  cfg = c;
  $("#sys-info").innerHTML = [
    row("Redis", h.redis ? "connected" : "unreachable", h.redis),
    row("Worker", h.worker ? "running" : "not seen", h.worker),
    row("ffmpeg", h.ffmpeg ? esc(shortPath(h.ffmpeg, 38)) : "missing", !!h.ffmpeg),
    row("JS runtime", h.js_runtime ? esc(h.js_runtime) : "none", !!h.js_runtime),
    row("yt-dlp", esc(h.yt_dlp)),
    row("Downloads", esc(shortPath(h.downloads_dir))),
    row("Timezone", esc(c.timezone)),
    row("Worker threads", String(c.workers)),
  ].join("");
}

async function openSettings() {
  const modal = bootstrap.Modal.getOrCreateInstance($("#settings-modal"));
  modal.show();
  try {
    const [rt, c] = await Promise.all([api.runtime(), cfg ? cfg : api.config()]);
    cfg = c;
    const slider = $("#s-par");
    slider.max = c.max_parallel_cap;
    slider.value = rt.max_parallel;
    setText($("#s-par-val"), String(rt.max_parallel));
    setText($("#s-workers"), String(c.workers));
    if (rt.speed_limit) {
      const mb = rt.speed_limit >= 1048576 && rt.speed_limit % 1048576 === 0;
      $("#s-speed-unit").value = mb ? "1048576" : "1024";
      $("#s-speed").value = Math.round(rt.speed_limit / (mb ? 1048576 : 1024));
    } else { $("#s-speed").value = ""; }
    await fillSystem();
  } catch (e) { toast(e.detail || e.message, "err"); }
}

async function save(btn) {
  try {
    const par = Number($("#s-par").value);
    const raw = Number($("#s-speed").value);
    const limit = raw > 0 ? Math.round(raw * Number($("#s-speed-unit").value)) : null;
    const rt = await withBusy(btn, () => api.setRuntime({ max_parallel: par, speed_limit: limit }));
    showRuntime(rt);
    toast("Settings applied — new downloads use them immediately.", "ok", { ms: 3000 });
    bootstrap.Modal.getInstance($("#settings-modal"))?.hide();
  } catch (e) { toast(e.detail || e.message, "err"); }
}

export function initSettings() {
  $("#open-settings").addEventListener("click", openSettings);
  $("#health-pill").addEventListener("click", openSettings);
  $("#s-par").addEventListener("input", (e) => setText($("#s-par-val"), e.target.value));
  $("#s-save").addEventListener("click", (e) => save(e.currentTarget));
}

/** Update the header pill + the "no worker" banner. */
export function renderHealth(h) {
  const pill = $("#health-pill");
  let cls = "ok", text = "All systems go";
  if (!h) { cls = "bad"; text = "Server offline"; }
  else if (!h.redis) { cls = "bad"; text = "Redis down"; }
  else if (!h.worker) { cls = "warn"; text = "No worker"; }
  else if (!h.ffmpeg) { cls = "warn"; text = "ffmpeg missing"; }
  pill.className = `health ${cls}`;
  pill.querySelector(".h-text").textContent = text;
  const tip = h ? (h.detail.length ? h.detail.join(" • ") : `yt-dlp ${h.yt_dlp} · ffmpeg ✓ · worker ✓`) : "Cannot reach the API";
  pill.setAttribute("data-bs-original-title", tip);
  pill.setAttribute("title", tip);
  const inst = bootstrap.Tooltip.getInstance(pill);
  if (inst) inst.setContent({ ".tooltip-inner": tip });

  const host = $("#banner-host");
  const msgs = [];
  if (h && !h.redis) msgs.push(`Redis is not reachable. Start it: <code>docker compose up -d redis</code>`);
  else if (h && !h.worker) msgs.push(`No download worker is running, so jobs will wait in the queue. Start one: <code>uv run python main.py worker</code>`);
  if (h && h.redis && !h.ffmpeg) msgs.push(`ffmpeg was not found — conversions will fail. Set <code>DL_FFMPEG_PATH</code> or install ffmpeg.`);
  host.innerHTML = msgs.map((m) => `<div class="banner"><i class="bi bi-exclamation-triangle-fill"></i><div>${m}</div></div>`).join("");
}
