// Reads this browser's youtube.com / google.com cookies and uploads them to the mst-ytdl server, using the one-time code.
const ext = typeof browser !== "undefined" ? browser : chrome;
const $ = (id) => document.getElementById(id);
const say = (text, cls = "") => { $("msg").textContent = text; $("msg").className = cls; };

const netscape = (c) => [
  c.domain, c.domain.startsWith(".") ? "TRUE" : "FALSE", c.path || "/", c.secure ? "TRUE" : "FALSE",
  c.expirationDate ? Math.floor(c.expirationDate) : 0, c.name, c.value,
].join("\t");

async function readCookies() {
  const jars = await Promise.all(["youtube.com", "google.com"].map((domain) => ext.cookies.getAll({ domain })));
  const seen = new Map();
  for (const c of jars.flat()) seen.set(`${c.domain}|${c.path}|${c.name}`, c);
  return [...seen.values()];
}

async function origin(server) {
  const url = new URL(server);
  if (!/^https?:$/.test(url.protocol)) throw new Error("The server address must start with http:// or https://");
  const pattern = url.origin + "/*";
  // already granted when the extension was downloaded from the site; otherwise ask once (this click is the user gesture)
  if (!(await ext.permissions.contains({ origins: [pattern] })) && !(await ext.permissions.request({ origins: [pattern] }))) {
    throw new Error("Permission to reach your server was not granted.");
  }
  return url.origin;
}

async function connect() {
  const code = $("code").value.trim();
  if (code.replace(/[^a-z0-9]/gi, "").length < 8) return say("Enter the 8-character code from the site.", "err");
  $("go").disabled = true;
  say("Working…");
  try {
    const base = await origin($("server").value.trim());
    const cookies = await readCookies();
    if (!cookies.some((c) => ["SID", "__Secure-3PSID", "LOGIN_INFO"].includes(c.name))) {
      throw new Error("You are not signed in to YouTube in this browser — sign in first.");
    }
    const text = "# Netscape HTTP Cookie File\n" + cookies.map(netscape).join("\n") + "\n";
    const res = await fetch(`${base}/api/connect/${encodeURIComponent(code)}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `The server said ${res.status}.`);
    await ext.storage.local.set({ server: base });
    say("Connected — go back to the site.", "ok");
  } catch (e) {
    say(e.message || String(e), "err");
  } finally {
    $("go").disabled = false;
  }
}

(async () => {
  let server = (await ext.storage.local.get("server")).server;
  if (!server) {
    try { server = (await (await fetch("config.json")).json()).server; } catch { /* unpacked from the repo: no baked-in address */ }
  }
  if (server) $("server").value = server;
  $("go").addEventListener("click", connect);
  $("code").addEventListener("keydown", (e) => { if (e.key === "Enter") connect(); });
  $("code").focus();
})();
