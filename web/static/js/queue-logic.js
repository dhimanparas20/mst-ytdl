// Pure player logic (no DOM): queue navigation, shuffle bag, reordering, quality choice, sleep timer.
// Kept separate so it can be unit-tested with `node --test web/tests`.

export const RATES = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
export const SLEEP_OPTIONS = [15, 30, 45, 60];  // minutes (plus "end of track")

const range = (n) => [...Array(n).keys()];

/** Every index except ``exclude`` — the tracks still to be played in a shuffle cycle. */
export const makeBag = (n, exclude = -1) => range(n).filter((i) => i !== exclude);

/**
 * Where to go after the current track.
 *
 * @param {{n:number,index:number,shuffle:boolean,repeat:'off'|'all'|'one',bag:number[]}} s
 * @param {() => number} [rnd] random source (injectable for tests)
 * @returns {{index:number|null, bag:number[]}} ``index`` is null at the end of the queue.
 *   Shuffle plays every track once per cycle (no repeats), then stops — or re-deals with repeat-all.
 */
export function nextIndex({ n, index, shuffle, repeat, bag }, rnd = Math.random) {
  if (!n) return { index: null, bag: [] };
  if (shuffle && n > 1) {
    let pool = bag.filter((i) => i < n && i !== index);
    if (!pool.length) {
      if (repeat !== "all") return { index: null, bag: [] };
      pool = makeBag(n, index);  // new cycle
    }
    const k = Math.floor(rnd() * pool.length);
    const pick = pool[k];
    return { index: pick, bag: pool.filter((_, j) => j !== k) };
  }
  if (index + 1 < n) return { index: index + 1, bag };
  return repeat === "all" ? { index: 0, bag } : { index: null, bag };
}

/** Move one queue entry; returns the new list and where the *current* track ended up. */
export function moveItem(list, from, to, index) {
  if (from === to || from < 0 || from >= list.length || to < 0 || to >= list.length) return { list, index };
  const next = list.slice();
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  let i = index;
  if (index === from) i = to;
  else if (from < index && to >= index) i = index - 1;
  else if (from > index && to <= index) i = index + 1;
  return { list: next, index: i };
}

/** Remove an entry; returns the list, the adjusted current index and whether the playing track was removed. */
export function removeItem(list, at, index) {
  if (at < 0 || at >= list.length) return { list, index, removedCurrent: false };
  const next = list.filter((_, i) => i !== at);
  if (at < index) return { list: next, index: index - 1, removedCurrent: false };
  if (at === index) return { list: next, index: Math.min(index, next.length - 1), removedCurrent: true };
  return { list: next, index, removedCurrent: false };
}

/** Randomise everything AFTER the current track (what is already played/playing stays put). */
export function shuffleRest(list, index, rnd = Math.random) {
  const head = list.slice(0, index + 1);
  const tail = list.slice(index + 1);
  for (let i = tail.length - 1; i > 0; i--) {
    const j = Math.floor(rnd() * (i + 1));
    [tail[i], tail[j]] = [tail[j], tail[i]];
  }
  return [...head, ...tail];
}

/**
 * Choose what to play for a quality preference.
 *
 * @param {{height:number}[]} qualities best first (the server's order)
 * @param {'auto'|'audio'|number|string} pref
 * @returns {{audio:true}|{audio:false, option:object}|null}
 */
export function pickQuality(qualities, pref) {
  if (pref === "audio") return { audio: true };
  if (!qualities?.length) return null;
  if (pref === "auto" || pref == null) {
    const ok = qualities.find((q) => q.height <= 720);  // "auto": the best that is cheap to stream
    return { audio: false, option: ok || qualities[qualities.length - 1] };
  }
  const want = Number(pref);
  const exact = qualities.find((q) => q.height === want);
  const lower = qualities.find((q) => q.height < want);  // nearest below
  return { audio: false, option: exact || lower || qualities[qualities.length - 1] };
}

/** Human label for the quality button. */
export function qualityLabel(pref, picked) {
  if (pref === "audio") return "Audio";
  if (!picked || picked.audio) return "Auto";
  return pref === "auto" || pref == null ? `Auto · ${picked.option.label}` : picked.option.label;
}

/** Seconds left on a sleep timer; ``null`` when none / "end of track" (no clock). */
export function sleepRemaining(sleep, now = Date.now()) {
  if (!sleep || sleep.mode !== "time") return null;
  return Math.max(0, Math.round((sleep.at - now) / 1000));
}

/** "mm:ss" for the sleep pill. */
export function fmtClock(sec) {
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Volume during the last seconds of a sleep timer (linear fade-out, never below 0). */
export function fadeVolume(base, remainingSec, fadeSec = 3) {
  if (remainingSec == null || remainingSec >= fadeSec) return base;
  return Math.max(0, base * (remainingSec / fadeSec));
}

/** Pick the subtitle track to switch on automatically: the remembered language, else none. */
export function pickSubtitle(tracks, preferredLang) {
  if (!preferredLang || !tracks?.length) return null;
  const base = (l) => String(l).toLowerCase().split("-")[0];
  return (
    tracks.find((t) => t.lang === preferredLang && !t.auto) ||
    tracks.find((t) => base(t.lang) === base(preferredLang) && !t.auto) ||
    tracks.find((t) => base(t.lang) === base(preferredLang)) ||
    null
  );
}

/** Clamp a floating window to the viewport (all numbers in px). */
export function clampWindow({ x, y, w, h }, vw, vh, margin = 8) {
  const width = Math.min(w, vw - margin * 2);
  const height = Math.min(h, vh - margin * 2);
  return {
    x: Math.min(Math.max(x, margin), Math.max(margin, vw - width - margin)),
    y: Math.min(Math.max(y, margin), Math.max(margin, vh - height - margin)),
    w: width,
  };
}

/** Snap to a viewport edge when within ``snap`` px of it. */
export function snapWindow({ x, y, w, h }, vw, vh, snap = 22, margin = 12) {
  let nx = x, ny = y;
  if (x < snap) nx = margin;
  else if (vw - (x + w) < snap) nx = vw - w - margin;
  if (y < snap) ny = margin;
  else if (vh - (y + h) < snap) ny = vh - h - margin;
  return { x: nx, y: ny };
}

/** Width that keeps 16:9 for a resize drag from a given corner. */
export function resizeFromCorner(start, corner, dx, minW, maxW) {
  const grow = corner.endsWith("r") ? dx : -dx;
  return Math.min(Math.max(start.w + grow, minW), maxW);
}

// ───────────── session speed stats ─────────────

/** Fold one speed sample (bytes/s) into ``{sum, n, peak}``; idle samples (0) are ignored, so the average is "while downloading". */
export function addSpeedSample(stats, speed) {
  const st = stats && Number.isFinite(stats.n) ? stats : { sum: 0, n: 0, peak: 0 };
  if (!(speed > 0)) return st;
  return { sum: st.sum + speed, n: st.n + 1, peak: Math.max(st.peak, speed) };
}

/** A delivery is "urgent" (pulse the Save button, warn colour) when it is deleted within this many seconds. */
export const SOON_SECONDS = 600;

/**
 * Is a server-mode delivery about to be deleted?
 * @param {number} expiresAt epoch seconds
 * @param {number} [nowSec] epoch seconds (injectable for tests)
 */
export const deliverySoon = (expiresAt, nowSec = Date.now() / 1000) => expiresAt - nowSec < SOON_SECONDS;

/** "available for 54 min" / "expires in under a minute" for a delivery's expiry. */
export function leftText(expiresAt, nowSec = Date.now() / 1000) {
  const s = Math.max(0, expiresAt - nowSec);
  if (s < 60) return "expires in under a minute";
  const m = Math.round(s / 60);
  return m >= 60 ? `available for ${Math.floor(m / 60)} h ${m % 60} min` : `available for ${m} min`;
}

/** Average speed of a stats object (0 when nothing was downloaded yet). */
export const speedAverage = (stats) => (stats && stats.n ? stats.sum / stats.n : 0);

/** Index of the line being sung at time ``t`` (binary search; -1 before the first). */
export function activeLine(lines, t) {
  let lo = 0, hi = lines.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (lines[mid].t <= t + 0.15) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}
