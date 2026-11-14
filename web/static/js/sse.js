// Live queue updates over Server-Sent Events, with reconnect and a polling fallback.

import { api } from "./api.js";

/**
 * @param {{onSnapshot:(s:{jobs:any[],stats:any})=>void, onStatus:(s:'live'|'reconnecting')=>void}} h
 * @returns {{close:()=>void}}
 */
export function connectEvents({ onSnapshot, onStatus }) {
  let es = null;
  let backoff = 1000;
  let poll = null;
  let closed = false;

  const statsFromJobs = (jobs) => {
    const c = (...k) => jobs.reduce((n, j) => n + k.reduce((m, x) => m + (j.counts?.[x] || 0), 0), 0);
    return {
      active: c("downloading", "processing"), queued: c("queued"), paused: c("paused"),
      completed: c("completed"), failed: c("failed"), cancelled: c("cancelled"),
      speed: jobs.reduce((n, j) => n + (j.speed || 0), 0),
      total_jobs: jobs.length, max_parallel: null, workers: null,
    };
  };

  function startPolling() {
    if (poll || closed) return;
    const tick = async () => {
      try {
        const jobs = await api.list({ limit: 200, include_items: true });
        onSnapshot({ jobs, stats: statsFromJobs(jobs) });
      } catch { /* server down — keep trying */ }
    };
    tick();
    poll = setInterval(tick, 2500);
  }
  const stopPolling = () => { if (poll) { clearInterval(poll); poll = null; } };

  function open() {
    if (closed) return;
    es = new EventSource("/api/events");
    es.addEventListener("snapshot", (e) => {
      backoff = 1000;
      stopPolling();
      onStatus("live");
      try { onSnapshot(JSON.parse(e.data)); } catch (err) { console.error("bad snapshot", err); }
    });
    es.onopen = () => { backoff = 1000; };
    es.onerror = () => {
      es.close();
      onStatus("reconnecting");
      startPolling();
      setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, 15000);
    };
  }

  open();
  return { close() { closed = true; stopPolling(); es && es.close(); } };
}
