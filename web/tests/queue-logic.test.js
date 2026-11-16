// node --test "web/tests/*.test.js"
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  clampWindow, fadeVolume, fmtClock, makeBag, moveItem, nextIndex, pickQuality, pickSubtitle, qualityLabel,
  activeLine, addSpeedSample, deliverySoon, leftText, speedAverage, removeItem, resizeFromCorner, shuffleRest, sleepRemaining, snapWindow,
} from "../static/js/queue-logic.js";

const Q = [{ height: 1080, label: "1080p" }, { height: 720, label: "720p" }, { height: 480, label: "480p" }, { height: 360, label: "360p" }, { height: 144, label: "144p" }];

// ───────── next track ─────────

test("linear order, end of queue, repeat-all wraps", () => {
  const s = { n: 3, index: 0, shuffle: false, repeat: "off", bag: [] };
  assert.equal(nextIndex(s).index, 1);
  assert.equal(nextIndex({ ...s, index: 2 }).index, null);
  assert.equal(nextIndex({ ...s, index: 2, repeat: "all" }).index, 0);
  assert.equal(nextIndex({ ...s, n: 0 }).index, null);
});

test("shuffle visits every track exactly once per cycle", () => {
  let bag = makeBag(6, 2);
  let index = 2;
  const seen = [index];
  for (let i = 0; i < 5; i++) {
    const r = nextIndex({ n: 6, index, shuffle: true, repeat: "off", bag });
    assert.notEqual(r.index, null);
    seen.push(r.index);
    index = r.index; bag = r.bag;
  }
  assert.deepEqual([...seen].sort(), [0, 1, 2, 3, 4, 5]);
  assert.equal(nextIndex({ n: 6, index, shuffle: true, repeat: "off", bag }).index, null);  // cycle over → stop
});

test("shuffle with repeat-all re-deals a new cycle and never repeats the current track back-to-back", () => {
  let bag = [], index = 0;
  for (let i = 0; i < 40; i++) {
    const r = nextIndex({ n: 4, index, shuffle: true, repeat: "all", bag }, Math.random);
    assert.notEqual(r.index, null);
    assert.notEqual(r.index, index);
    index = r.index; bag = r.bag;
  }
});

test("shuffle is deterministic with an injected random source", () => {
  const r = nextIndex({ n: 5, index: 0, shuffle: true, repeat: "off", bag: [1, 2, 3, 4] }, () => 0.99);
  assert.equal(r.index, 4);
  assert.deepEqual(r.bag, [1, 2, 3]);
});

test("a one-track queue does not loop forever in shuffle mode", () => {
  assert.equal(nextIndex({ n: 1, index: 0, shuffle: true, repeat: "off", bag: [] }).index, null);
});

test("stale bag entries (queue shrank) are ignored", () => {
  const r = nextIndex({ n: 3, index: 0, shuffle: true, repeat: "off", bag: [1, 2, 7, 9] }, () => 0);
  assert.ok([1, 2].includes(r.index));
});

// ───────── reorder / remove ─────────

test("moveItem keeps the playing track tracked", () => {
  const L = ["a", "b", "c", "d", "e"];
  assert.deepEqual(moveItem(L, 3, 0, 1), { list: ["d", "a", "b", "c", "e"], index: 2 });  // moved above current
  assert.deepEqual(moveItem(L, 0, 4, 2), { list: ["b", "c", "d", "e", "a"], index: 1 });  // moved below current
  assert.deepEqual(moveItem(L, 1, 3, 1), { list: ["a", "c", "d", "b", "e"], index: 3 });  // current itself moved
  assert.deepEqual(moveItem(L, 3, 4, 1), { list: ["a", "b", "c", "e", "d"], index: 1 });  // unrelated move
  assert.deepEqual(moveItem(L, 2, 2, 1), { list: L, index: 1 });
  assert.deepEqual(moveItem(L, -1, 2, 1), { list: L, index: 1 });
  assert.deepEqual(moveItem(L, 1, 9, 1), { list: L, index: 1 });
});

test("removeItem adjusts the current index", () => {
  const L = ["a", "b", "c", "d"];
  assert.deepEqual(removeItem(L, 0, 2), { list: ["b", "c", "d"], index: 1, removedCurrent: false });
  assert.deepEqual(removeItem(L, 3, 1), { list: ["a", "b", "c"], index: 1, removedCurrent: false });
  assert.deepEqual(removeItem(L, 2, 2), { list: ["a", "b", "d"], index: 2, removedCurrent: true });
  assert.deepEqual(removeItem(L, 3, 3), { list: ["a", "b", "c"], index: 2, removedCurrent: true });
  assert.deepEqual(removeItem(L, 9, 1), { list: L, index: 1, removedCurrent: false });
});

test("shuffleRest keeps played tracks and the current one in place", () => {
  const L = [1, 2, 3, 4, 5, 6, 7, 8];
  const out = shuffleRest(L, 2);
  assert.deepEqual(out.slice(0, 3), [1, 2, 3]);
  assert.deepEqual([...out.slice(3)].sort((a, b) => a - b), [4, 5, 6, 7, 8]);
  assert.deepEqual(shuffleRest([1], 0), [1]);
  assert.deepEqual(shuffleRest([1, 2, 3], 2), [1, 2, 3]);
  assert.notDeepEqual(shuffleRest(Array.from({ length: 50 }, (_, i) => i), 0).slice(1), Array.from({ length: 49 }, (_, i) => i + 1));
});

// ───────── quality ─────────

test("pickQuality: auto prefers ≤720p, explicit picks exact then nearest lower", () => {
  assert.equal(pickQuality(Q, "auto").option.height, 720);
  assert.equal(pickQuality(Q, undefined).option.height, 720);
  assert.equal(pickQuality(Q, 1080).option.height, 1080);
  assert.equal(pickQuality(Q, "480").option.height, 480);
  assert.equal(pickQuality(Q, 600).option.height, 480);   // nearest below
  assert.equal(pickQuality(Q, 100).option.height, 144);   // nothing lower → lowest available
  assert.equal(pickQuality(Q, 2160).option.height, 1080); // asked for more than exists
  assert.deepEqual(pickQuality(Q, "audio"), { audio: true });
  assert.equal(pickQuality([], "auto"), null);
  assert.deepEqual(pickQuality([], "audio"), { audio: true });
  assert.equal(pickQuality([{ height: 1080, label: "1080p" }], "auto").option.height, 1080);  // only high → still plays
});

test("qualityLabel", () => {
  assert.equal(qualityLabel("auto", pickQuality(Q, "auto")), "Auto · 720p");
  assert.equal(qualityLabel(1080, pickQuality(Q, 1080)), "1080p");
  assert.equal(qualityLabel("audio", { audio: true }), "Audio");
  assert.equal(qualityLabel("auto", null), "Auto");
});

// ───────── sleep timer ─────────

test("sleep timer maths", () => {
  assert.equal(sleepRemaining(null), null);
  assert.equal(sleepRemaining({ mode: "track" }), null);
  assert.equal(sleepRemaining({ mode: "time", at: 10_000 }, 4_000), 6);
  assert.equal(sleepRemaining({ mode: "time", at: 10_000 }, 20_000), 0);
  assert.equal(fmtClock(0), "0:00");
  assert.equal(fmtClock(75), "1:15");
  assert.equal(fmtClock(3600), "60:00");
});

test("fadeVolume only fades inside the last seconds and never goes negative", () => {
  assert.equal(fadeVolume(0.8, null), 0.8);
  assert.equal(fadeVolume(0.8, 10), 0.8);
  assert.equal(fadeVolume(0.9, 3), 0.9);
  assert.ok(Math.abs(fadeVolume(0.9, 1.5) - 0.45) < 1e-9);
  assert.equal(fadeVolume(0.9, 0), 0);
  assert.equal(fadeVolume(0.9, -2), 0);
});

// ───────── subtitles ─────────

test("pickSubtitle prefers an exact manual track over auto-generated ones", () => {
  const T = [{ lang: "en", auto: true }, { lang: "en", auto: false }, { lang: "hi", auto: false }, { lang: "en-orig", auto: true }];
  assert.deepEqual(pickSubtitle(T, "en"), { lang: "en", auto: false });
  assert.deepEqual(pickSubtitle(T, "hi"), { lang: "hi", auto: false });
  assert.equal(pickSubtitle(T, "fr"), null);
  assert.equal(pickSubtitle(T, null), null);
  assert.equal(pickSubtitle([], "en"), null);
  assert.deepEqual(pickSubtitle([{ lang: "en-GB", auto: false }], "en"), { lang: "en-GB", auto: false });  // regional variant
  assert.deepEqual(pickSubtitle([{ lang: "en", auto: true }], "en"), { lang: "en", auto: true });           // auto is better than nothing
});

// ───────── floating window geometry ─────────

test("clampWindow keeps the window fully on screen", () => {
  assert.deepEqual(clampWindow({ x: -50, y: -50, w: 400, h: 250 }, 1000, 800), { x: 8, y: 8, w: 400 });
  assert.deepEqual(clampWindow({ x: 900, y: 790, w: 400, h: 250 }, 1000, 800), { x: 592, y: 542, w: 400 });
  assert.deepEqual(clampWindow({ x: 10, y: 10, w: 2000, h: 250 }, 1000, 800), { x: 8, y: 10, w: 984 });
});

test("snapWindow sticks to nearby edges only", () => {
  assert.deepEqual(snapWindow({ x: 10, y: 400, w: 300, h: 200 }, 1000, 800), { x: 12, y: 400 });
  assert.deepEqual(snapWindow({ x: 690, y: 595, w: 300, h: 200 }, 1000, 800), { x: 688, y: 588 });
  assert.deepEqual(snapWindow({ x: 300, y: 300, w: 300, h: 200 }, 1000, 800), { x: 300, y: 300 });
});

test("resizeFromCorner grows right corners with +dx and left corners with -dx, within limits", () => {
  const start = { w: 400 };
  assert.equal(resizeFromCorner(start, "br", 100, 260, 900), 500);
  assert.equal(resizeFromCorner(start, "tr", 100, 260, 900), 500);
  assert.equal(resizeFromCorner(start, "bl", 100, 260, 900), 300);
  assert.equal(resizeFromCorner(start, "tl", -100, 260, 900), 500);
  assert.equal(resizeFromCorner(start, "br", -999, 260, 900), 260);
  assert.equal(resizeFromCorner(start, "br", 999, 260, 900), 900);
});

// ───────── session speed stats ─────────
test("speed stats ignore idle samples and track average + peak", () => {
  let st = addSpeedSample(undefined, 0);
  assert.deepEqual(st, { sum: 0, n: 0, peak: 0 });
  assert.equal(speedAverage(st), 0);
  st = addSpeedSample(st, 1000);
  st = addSpeedSample(st, 0);
  st = addSpeedSample(st, 3000);
  assert.equal(st.n, 2);
  assert.equal(speedAverage(st), 2000);
  assert.equal(st.peak, 3000);
  assert.deepEqual(addSpeedSample({ junk: true }, 5), { sum: 5, n: 1, peak: 5 });  // corrupt saved state is replaced
  assert.equal(addSpeedSample(st, -4), st);                                       // negatives / NaN ignored
  assert.equal(addSpeedSample(st, NaN), st);
});

test("activeLine: the lyric line being sung", () => {
  const L = [{ t: 0 }, { t: 5.2 }, { t: 9 }, { t: 9 }, { t: 14.5 }];
  assert.equal(activeLine([], 3), -1);
  assert.equal(activeLine([{ t: 2 }], 1), -1);          // before the first line
  assert.equal(activeLine(L, 0), 0);
  assert.equal(activeLine(L, 5.1), 1);                  // 150 ms lead so the highlight is not late
  assert.equal(activeLine(L, 8.99), 3);                 // equal stamps: the last one wins
  assert.equal(activeLine(L, 1000), 4);
});

test("deliverySoon: urgent inside the last 10 minutes (and once expired)", () => {
  const now = 1_000_000;
  assert.equal(deliverySoon(now + 3600, now), false);
  assert.equal(deliverySoon(now + 601, now), false);
  assert.equal(deliverySoon(now + 599, now), true);
  assert.equal(deliverySoon(now - 5, now), true);
});

test("leftText: minutes, hours, under a minute", () => {
  const now = 5000;
  assert.equal(leftText(now + 30, now), "expires in under a minute");
  assert.equal(leftText(now - 30, now), "expires in under a minute");
  assert.equal(leftText(now + 54 * 60, now), "available for 54 min");
  assert.equal(leftText(now + 3600, now), "available for 1 h 0 min");
  assert.equal(leftText(now + 95 * 60, now), "available for 1 h 35 min");
});
