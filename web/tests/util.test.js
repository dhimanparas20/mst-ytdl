// Run with:  node --test web/tests
// Pure helpers only (no DOM): formatting, speed units, playlist-selection syntax, URL picking.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  compressSelection, cookieChipState, esc, extractUrls, fmtBytes, fmtDuration, fmtEta, fmtSpeed, parseSelection, splitSpeed,
} from "../static/js/util.js";

test("speed is shown in bytes/s and scales B → KB → MB → GB (never bits)", () => {
  assert.deepEqual(splitSpeed(0), { value: "0", unit: "B/s" });
  assert.deepEqual(splitSpeed(512), { value: "512", unit: "B/s" });
  assert.deepEqual(splitSpeed(1024), { value: "1.0", unit: "KB/s" });
  assert.deepEqual(splitSpeed(87.8 * 1024), { value: "87.8", unit: "KB/s" });
  assert.deepEqual(splitSpeed(850 * 1024), { value: "850", unit: "KB/s" });
  assert.deepEqual(splitSpeed(1.2 * 1024 ** 2), { value: "1.2", unit: "MB/s" });
  assert.deepEqual(splitSpeed(25 * 1024 ** 2), { value: "25.0", unit: "MB/s" });
  assert.deepEqual(splitSpeed(3 * 1024 ** 3), { value: "3.0", unit: "GB/s" });
  for (const n of [1, 999, 5000, 5e6, 5e9]) assert.doesNotMatch(splitSpeed(n).unit, /bit/i);
});

test("the unit steps up exactly at 1024", () => {
  assert.equal(splitSpeed(1023).unit, "B/s");
  assert.equal(splitSpeed(1024).unit, "KB/s");
  assert.equal(splitSpeed(1024 ** 2 - 1).unit, "KB/s");
  assert.equal(splitSpeed(1024 ** 2).unit, "MB/s");
});

test("fmtSpeed / fmtBytes", () => {
  assert.equal(fmtSpeed(0), "—");
  assert.equal(fmtSpeed(2 * 1024 ** 2), "2.0 MB/s");
  assert.equal(fmtBytes(null), "—");
  assert.equal(fmtBytes(0), "0 B");
  assert.equal(fmtBytes(1536), "1.5 KB");
  assert.equal(fmtBytes(5 * 1024 ** 3), "5.0 GB");
});

test("fmtDuration / fmtEta", () => {
  assert.equal(fmtDuration(null), "");
  assert.equal(fmtDuration(65), "1:05");
  assert.equal(fmtDuration(3725), "1:02:05");
  assert.equal(fmtEta(45), "45s");
  assert.equal(fmtEta(125), "2m 5s");
  assert.equal(fmtEta(7380), "2h 3m");
  assert.equal(fmtEta(undefined), "");
});

test("compressSelection mirrors the backend's playlist_items syntax", () => {
  assert.equal(compressSelection([1, 2, 3, 5, 7, 8]), "1-3,5,7-8");
  assert.equal(compressSelection([5, 1, 3, 2]), "1-3,5");
  assert.equal(compressSelection([4, 4, 4]), "4");
  assert.equal(compressSelection([]), "");
});

test("parseSelection accepts ranges, open ends and rejects garbage", () => {
  assert.deepEqual([...parseSelection("1,3,5-7", 10)], [1, 3, 5, 6, 7]);
  assert.deepEqual([...parseSelection("8-", 10)], [8, 9, 10]);
  assert.deepEqual([...parseSelection("-3", 10)], [1, 2, 3]);
  assert.deepEqual([...parseSelection(" 2 , 4 ", 10)], [2, 4]);
  assert.deepEqual([...parseSelection("0, 99", 10)], []);       // out of range → empty, not an error
  assert.equal(parseSelection("abc", 10), null);
  assert.equal(parseSelection("5-3", 10), null);
  assert.equal(parseSelection("1-2-3", 10), null);
  assert.deepEqual([...parseSelection("", 10)], []);
});

test("compress → parse round-trips", () => {
  const picked = [1, 2, 3, 10, 12, 13];
  assert.deepEqual([...parseSelection(compressSelection(picked), 20)], picked);
});

test("extractUrls keeps only YouTube / YouTube Music links", () => {
  const text = "hello https://www.youtube.com/watch?v=dQw4w9WgXcQ\nhttps://music.youtube.com/playlist?list=PL1  youtu.be/abc12345678 https://vimeo.com/1 notaurl";
  assert.deepEqual(extractUrls(text), [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/playlist?list=PL1",
    "youtu.be/abc12345678",
  ]);
  assert.deepEqual(extractUrls(""), []);
  assert.deepEqual(extractUrls("https://notyoutube.com/watch?v=x"), []);
});

test("esc neutralises HTML", () => {
  assert.equal(esc(`<img src=x onerror="alert('x')">&`), "&lt;img src=x onerror=&quot;alert(&#39;x&#39;)&quot;&gt;&amp;");
  assert.equal(esc(null), "");
});

test("cookie chip: none / not signed in / signed in / expiring / expired", () => {
  const now = Date.parse("2026-01-01T00:00:00Z");
  const day = 86400000;
  const base = { loaded: true, source: "file", count: 5, login: true, warnings: [], expires_at: null };
  assert.equal(cookieChipState(null, now), null);
  assert.deepEqual(cookieChipState({ loaded: false }, now).text, "Cookies · none");
  assert.equal(cookieChipState({ loaded: false }, now).tone, "none");
  assert.equal(cookieChipState({ ...base, login: false }, now).text, "Cookies · not signed in");
  assert.equal(cookieChipState({ ...base, source: "browser", count: 0, browser: "firefox" }, now).text, "Cookies · browser");
  assert.deepEqual(
    (({ text, tone }) => ({ text, tone }))(cookieChipState(base, now)),
    { text: "Cookies · signed in", tone: "ok" },
  );
  const far = cookieChipState({ ...base, expires_at: new Date(now + 90 * day).toISOString() }, now);
  assert.equal(far.text, "Cookies · signed in");
  const soon = cookieChipState({ ...base, expires_at: new Date(now + 10 * day + 1000).toISOString() }, now);
  assert.equal(soon.text, "Cookies · signed in · 10 d left");
  assert.equal(soon.tone, "ok");
  const imminent = cookieChipState({ ...base, expires_at: new Date(now + 2 * day).toISOString() }, now);
  assert.equal(imminent.tone, "warn");
  assert.equal(cookieChipState({ ...base, expires_at: new Date(now + 3600000).toISOString() }, now).text, "Cookies · signed in · <1 d left");
  assert.equal(cookieChipState({ ...base, expires_at: new Date(now - day).toISOString() }, now).text, "Cookies · expired");
  assert.equal(cookieChipState({ ...base, warnings: ["x"] }, now).tone, "warn");
});
