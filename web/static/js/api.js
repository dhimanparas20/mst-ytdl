// Thin fetch wrapper around the FastAPI backend.

export class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; this.detail = detail; }
}

function explain(body, fallback) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`.replace(/^: /, "")).join("; ");
  return fallback;
}

async function request(method, path, body) {
  let res;
  try {
    res = await fetch("/api" + path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError(0, "Cannot reach the server — is it running?");
  }
  if (!res.ok) {
    let j = null;
    try { j = await res.json(); } catch { /* not JSON */ }
    throw new ApiError(res.status, explain(j, res.statusText || `HTTP ${res.status}`));
  }
  return res.status === 204 ? null : res.json();
}

const qs = (o) => {
  const p = new URLSearchParams();
  Object.entries(o || {}).forEach(([k, v]) => v !== undefined && v !== null && v !== "" && p.set(k, v));
  const s = p.toString();
  return s ? "?" + s : "";
};

export const api = {
  health: () => request("GET", "/health"),
  config: () => request("GET", "/config"),
  runtime: () => request("GET", "/runtime"),
  setRuntime: (body) => request("PUT", "/runtime", body),
  cookies: () => request("GET", "/cookies"),
  saveCookies: (text) => request("PUT", "/cookies", { text }),
  clearCookies: () => request("DELETE", "/cookies"),
  testCookies: () => request("POST", "/cookies/test"),

  info: (url, playlist_mode = "auto") => request("POST", "/info", { url, playlist_mode }),
  search: (q, music = false, limit = 12) => request("GET", "/search" + qs({ q, music: music || undefined, limit })),

  create: (body) => request("POST", "/downloads", body),
  list: (params) => request("GET", "/downloads" + qs(params)),
  job: (id) => request("GET", `/downloads/${id}`),
  jobAction: (id, action) => request("POST", `/downloads/${id}/${action}`),
  itemAction: (id, action) => request("POST", `/items/${id}/${action}`),
  remove: (id, deleteFiles = false) => request("DELETE", `/downloads/${id}${qs({ delete_files: deleteFiles || undefined })}`),
  queue: (action, deleteFiles = false) => request("POST", `/queue/${action}${qs({ delete_files: deleteFiles || undefined })}`),

  bulk: (ids, action) => request("POST", "/downloads/bulk", { ids, action }),

  // in-app playback (YouTube streams proxied by our server — no YouTube iframe, nothing is "embed-blocked")
  streamInfo: (videoId) => request("GET", `/stream/${encodeURIComponent(videoId)}/info`),
  streamUrl: (videoId, formatId) => `/api/stream/${encodeURIComponent(videoId)}/media?format=${encodeURIComponent(formatId)}`,
  streamSubsUrl: (videoId, lang, auto) => `/api/stream/${encodeURIComponent(videoId)}/subs?lang=${encodeURIComponent(lang)}&auto=${!!auto}`,
  libSubtitles: (path) => request("GET", "/library/subtitles" + qs({ path })),
  libSubtitleUrl: (path, track) => "/api/library/subtitle" + qs({ path, track }),

  library: (path = "") => request("GET", "/library" + qs({ path })),
  libUsage: () => request("GET", "/library/usage"),
  libFolders: () => request("GET", "/library/folders"),
  libDelete: (path) => request("DELETE", "/library" + qs({ path })),
  libBulkDelete: (paths) => request("POST", "/library/bulk-delete", { paths }),
  libMove: (paths, dest) => request("POST", "/library/move", { paths, dest }),
  libRename: (path, name) => request("POST", "/library/rename", { path, name }),
  fileUrl: (path, download = false) => "/api/library/file" + qs({ path, download: download || undefined }),
  /** One path or many → a ZIP download URL (the parameter repeats). */
  zipUrl: (paths) => "/api/library/zip?" + [].concat(paths).map((p) => "path=" + encodeURIComponent(p)).join("&"),
};
