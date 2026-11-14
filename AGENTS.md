# AGENTS.md — project conventions & architecture

> Read this first. It is the single source of truth for architecture, layering and hard rules.
> If this file and the code disagree, fix one of them before continuing.

---

## §0. Self-maintenance rules

**0.1 Update docs side by side with code.** Any change to code, settings, endpoints, tasks or containers updates, in the same change:

- settings class / env var / default → §3, the env table in `README.md`, and `.env.sample`
- client getter → §4 and the `configs/clients.py` docstring
- service → §5 table and `services/__init__.py` re-exports
- model → §6
- task → §7
- endpoint → the API table in `README.md`
- container / volume / build step → `compose.yml`, `Dockerfile`, §8
- new gotcha → §10 (with the reason)

**0.2 Never commit secrets.** `.env`, cookie files and anything under `downloads/` are gitignored.

**0.3 Match the style.** Google-style docstrings (Args/Returns/Raises), `from __future__ import annotations`, `lower_snake_case`, a module docstring at the top of every file, type hints everywhere.

---

## §1. What this is

A self-hosted YouTube / YouTube Music downloader. FastAPI serves the API and the web UI; **Huey** (Redis-backed) runs downloads in a separate consumer process; **Redis** holds job/item state, the parallel-download slots and runtime settings; **yt-dlp + ffmpeg** do the media work.

Stack: Python 3.14 · FastAPI · Huey 3 · pydantic v2 + pydantic-settings · redis-py · yt-dlp (+ bundled ffmpeg via imageio-ffmpeg) · uv · Docker Compose · Tailwind (CDN, no preflight) + Bootstrap 5 + vanilla ES modules.

Output layout: single video → `downloads/<today>/Title.ext`; playlist → `downloads/<Playlist name>/NN - Title.ext`.

---

## §2. Layered architecture (where things go)

Dependencies only point downward:

```
api/ (FastAPI)  →  tasks/ (Huey)  →  services/  →  configs/  →  modules/
                       models/ (pure data, used by all of the above)
```

| Layer | Directory | Belongs here | Does NOT belong here |
|---|---|---|---|
| Settings | `configs/config.py` | `BaseSettings` per domain, aggregated in `Settings`, cached `get_settings()` | clients, I/O |
| Clients | `configs/clients.py` | `get_*()` singletons (`get_redis`, `get_ffmpeg_path`, `get_js_runtimes`) | business logic |
| Generic helpers | `modules/` | project-agnostic helpers: logger, time/size formatting, URL parsing, filename safety, redis (de)serialisation | anything that needs settings or models |
| Models | `models/` | pydantic requests/records/views, enums, pure derivations | redis, yt-dlp, settings |
| Services | `services/` | all business logic, singletons (`download_manager`, …) | huey decorators; importing `tasks`/`api` |
| Tasks | `tasks/` | thin Huey tasks + dispatcher registration | logic — delegate to services |
| API | `api/` | routers, schemas, error mapping, static/UI mounting | logic — call services |
| UI | `web/` | `index.html`, `static/css`, `static/js` (ES modules) | — |
| Tests | `tests/` | mirrors the layers | — |

**Why a dispatcher:** the manager must enqueue work but services may not import `tasks`. `services/dispatch.py` defines the seam; `tasks/tasks.py` registers `HueyDispatcher` on import; tests register `tests.fakes.FakeDispatcher`. `api/main.py` imports `tasks` so the registration always happens.

Placement checklists

- *New setting:* small class (or field) in `configs/config.py` with an `env_prefix` → `.env.sample` → §3 + README.
- *New client:* `@lru_cache` getter in `configs/clients.py`, re-export in `configs/__init__.py`; never construct clients ad hoc.
- *New service:* `services/<name>.py`, class + module singleton, re-export in `services/__init__.py`.
- *New task:* in `tasks/tasks.py`, thin; export in `tasks/__init__.py`.
- *New model:* `models/<domain>.py`, re-export in `models/__init__.py` (call sites do `from models import X`).

---

## §3. Config system (`configs/config.py`)

Resolution: real env vars > `.env` (absolute path, CWD-independent) > field defaults. One `BaseSettings` per domain, each with an `env_prefix`:

| Class | Prefix | Examples |
|---|---|---|
| `RedisSettings` | – | `REDIS_URL`, `REDIS_KEY_PREFIX` |
| `FastAPISettings` | `api_` | `API_HOST` (default `127.0.0.1`), `API_PORT`, `API_CORS_ORIGINS` |
| `DownloadSettings` | `dl_` | `DL_DOWNLOADS_DIR`, `DL_TIMEZONE`, `DL_SPEED_LIMIT` (`2M`), `DL_ALLOWED_HOSTS`, `DL_COOKIES_*`, `DL_DEFAULT_FOLDER`, `DL_PLAYLIST_NUMBERING` |
| `TaskSettings` | `task_` | `TASK_WORKERS`, `TASK_MAX_PARALLEL(_CAP)`, `TASK_RETRY_*`, `TASK_LOG_PROGRESS`, `TASK_IMMEDIATE` |

Access with `get_settings()` (cached; never construct `Settings()` ad hoc). Blank `KEY=` in `.env` is treated as unset for the optional fields. In tests, mutate the nested instance with `monkeypatch.setattr(get_settings().download, "downloads_dir", path)`.

**Runtime-adjustable settings** (parallel limit, speed cap, default folder) live in Redis (`P:RUNTIME`, `services/runtime.py`), layered over the `.env` defaults and clamped to `TASK_MAX_PARALLEL_CAP`. The Huey thread count (`TASK_WORKERS`) is the hard ceiling and needs a worker restart.

---

## §4. Clients (`configs/clients.py`)

| Getter | Returns | Notes |
|---|---|---|
| `get_redis()` | sync `redis.Redis` (`decode_responses=True`) | lazy; tests swap it with `set_redis_override(fakeredis…)` |
| `get_key_prefix()` | `str` | prefix of every key |
| `get_ffmpeg_path()` | `str \| None` | configured → PATH → bundled imageio-ffmpeg |
| `get_js_runtimes()` | `dict \| None` | `{"node": {"path": …}}` for yt-dlp's `js_runtimes` |

Huey workers are synchronous; API endpoints that touch Redis are plain `def` (FastAPI runs them in its threadpool). Only the SSE generator is `async` and offloads with `run_in_threadpool`.

---

## §5. Services (`services/`)

| File | Singleton | Responsibility |
|---|---|---|
| `store.py` | `job_store` | Redis persistence. Jobs/items are **hashes with one JSON value per field**, so workers patch `percent`/`speed` without racing the API. `transition_item(id, allowed, updates, expect_token=)` is an optimistic WATCH/MULTI transaction — the only safe way to change status. Keys: `P:JOB:<id>`, `P:ITEM:<id>`, `P:JOB_ITEMS:<job>` (list), `P:JOBS` (zset by created_at). TTL = `TASK_RECORD_RETENTION_DAYS` |
| `runtime.py` | `runtime_service`, `slot_limiter` | Live settings; cross-process parallel slots (zset `P:SLOTS` + per-holder heartbeat key with TTL; dead holders are reclaimed). Holders are `"<item_id>:<run_id>"` |
| `metadata.py` | `metadata_service` | URL → allow-list → single/playlist decision; `preview`, `expand_playlist`, `search`; the only place that calls yt-dlp for metadata (`_extract`) |
| `ytdl_opts.py` | functions | Option mapping: `format_selector`, `format_sort`, `build_postprocessors`, `build_ydl_opts`, `friendly_error` |
| `downloader.py` | `download_runner` | Runs ONE item: stale check → wait for slot → atomic claim → yt-dlp with hooks → settle (completed / paused / cancelled / failed / retry) |
| `manager.py` | `download_manager` | Orchestration: `submit`, `resolve_job`, pause/resume/cancel/retry (item, job, bulk), delete, reads, `snapshot`, `reconcile` |
| `cookies.py` | `cookie_service` | Web-UI cookies: `save(text)` validates Netscape text (`modules/cookies_util.py`), keeps only youtube/google domains, atomic 0600 write to `DownloadSettings.cookies_path`; `status()` (counts/flags, **never values**); `test()` = live yt-dlp check of a public video; `clear()`. `ytdl_opts` passes the file to yt-dlp only if it exists (checked per call → no restart); the API clears the stream cache on change |
| `library.py` | `library_service` | Browse/serve/zip/delete under the downloads root; `safe_path` rejects anything escaping it |
| `dispatch.py` | – | `Dispatcher` protocol + `set_dispatcher/get_dispatcher` |
| `errors.py` | – | `NotFoundError` (404) · `ConflictError` (409) · `InvalidRequestError` (422) |

### State machine (item)

```
queued ──claim──▶ downloading ──▶ processing ──▶ completed
   │                  │   │
   │ pause (direct)   │   └─ control=pause ─▶ paused ──resume──▶ queued
   ▼                  └──── control=cancel ─▶ cancelled ──retry──▶ queued
 paused / cancelled                  failed ──retry──▶ queued   (auto-retry for transient errors)
```

- `token`: every (re)queue gets a fresh token; a task whose token no longer matches exits as *stale*. This is how pausing a queued item, duplicate deliveries and retries stay safe.
- `control`: a pending pause/cancel request on a *running* item; honoured by the progress hook (throttled ≈2×/s).
- Job status is **derived** from items (`models.derive_job_status`), never stored. Playlist expansion state is `resolve_state` on the job.
- Skip-existing: the runner predicts the final filename (template + target extension) and completes immediately if it exists and is non-empty, unless `overwrite=true`.

---

## §6. Models (`models/`)

- `enums.py` – `MediaFormat` (mp3/m4a/opus/flac/wav/mp4/mkv/webm; `.is_audio`, `.lossy_bitrate`), `DownloadStatus`, `ControlAction`, `JobKind`, `PlaylistMode`, `ResolveState`, quality tuples.
- `download.py` – `DownloadOptions` (normalises `1080p`/`320k`, validates quality per format family), `DownloadRequest`, `DownloadJob`, `DownloadItem` (incl. `token`, `control`, `partials`), `JobView.build(job, items)` (aggregates), `MediaInfo`/`EntryInfo`, `RuntimeSettings`, `QueueStats`, `derive_job_status`.
- Models are pure data + validation; no redis/yt-dlp/settings imports. Defaults from settings are applied in `DownloadManager.resolve_options`.

---

## §7. Tasks (`tasks/tasks.py`)

One `RedisHuey("mst-ytdl", results=False, immediate=TASK_IMMEDIATE)`. Tasks: `resolve_job_task`, `download_item_task`, periodic `reconcile_task` (every minute; also publishes the worker heartbeat `P:WORKER:HEARTBEAT` that `/api/health` reads).

Conventions: tasks never raise for expected outcomes (failed/paused/cancelled are *recorded*), so Huey never retries on its own — retries are scheduled by the runner (`dispatcher.enqueue_item(..., delay=TASK_RETRY_DELAY)`). `on_startup` runs once per worker thread, so it is guarded by an event.

Run the consumer with threads (`-k thread`); it works on Windows. `uv run python main.py worker` does this.

---

## §8. Run, containers, tests

- `uv run python main.py worker | api | dev`
- `compose.yml`: `redis`, `huey`, `app` (no profiles, so `up`/`down` act on all three; `docker compose up -d redis` for dev); `./downloads` is mounted into both. `Dockerfile` installs ffmpeg + nodejs.
- Tests: `uv run pytest` (fast; `TASK_IMMEDIATE=true` set in `tests/conftest.py`, fake Redis per test, `tests/fakes.py::FakeYDL` scripted yt-dlp) and `uv run pytest -m live` (real network). Lint: `uv run ruff check .`.
- **Default `pytest` deselects `live`** (`addopts` in `pyproject.toml`).

---

## §9. Web UI (`web/`)

Single page: `index.html` + `static/css/app.css` (design tokens, glass, animations) + ES modules in `static/js/`:

| Module | Role |
|---|---|
| `app.js` | boot, tabs (desktop 2-column / mobile bottom-nav), health polling, SSE wiring |
| `queue.js` | stats tiles + sparkline, toolbar, filters, **keyed in-place job/item DOM updates** (no re-render of the list per snapshot) |
| `composer.js` | link/search input, format & quality chips, option switches, preview, playlist track picker, submit |
| `library.js` | folder browser, player modal, zip/delete |
| `settings.js` | settings modal, health pill + "no worker" banner |
| `sse.js` | EventSource with backoff and a polling fallback |
| `api.js`, `ui.js`, `util.js` | fetch wrapper, toasts/confirm/ripple, helpers |

Rules: never inject untrusted text with `innerHTML` without `esc()`; a snapshot carries items only for unfinished jobs and single-video jobs (finished playlists are fetched on demand); static assets are served with `Cache-Control: no-cache` (ETag revalidation).

---

## §9b. Player, streaming, temp dirs (added later — read before touching these)

- `services/streaming.py` (`stream_service`): `info(video_id)` (cached ~20 min, `build_info` is pure) → `QualityOption`s (muxed = one file; otherwise video-only + audio-only), `open_media()` relays ONE ≤8 MiB ranged chunk from googlevideo, `subtitle_vtt()`. Client never supplies upstream URLs (video id regex + format id must exist in the cached yt-dlp result; hosts limited to googlevideo/youtube).
- **CDN quirks (verified live):** small header ranges (`Range: bytes=0-65535`) 403 on large adaptive streams, and a freshly signed URL is refused for ~1–2 s. So: use the `&range=a-b` query parameter with the exact size from the URL's `clen`; retry 401/403 up to `_ATTEMPTS` with `_RETRY_DELAY_S`; drop the cache on final failure.
- Every download writes into a private `downloads/.tmp/<item_id>/` (yt-dlp `paths.temp`); only the finished file is moved to its folder. This prevents same-title parallel jobs (mp3+mp4+opus) from colliding on thumbnails/intermediates, makes cancel = delete one directory, and resume independent of file names. Dot-names are hidden from the library/zip/usage. The destination folder is NOT pre-created (no empty folders after failures).
- Item `force` (set by *restart*) = ignore an existing output and overwrite; cleared when the run ends, kept across auto-retry.
- **Dual-stream (HD) player loop — never re-seek the picture on every `playing`.** Setting `V.currentTime` makes the video emit `waiting`; the handler then pauses the audio clock, `playing` resumes it, and the cycle repeats ~70×/s with the clock frozen at 0:00 (reproduced with an event log). Re-seek only on real drift (>0.3 s) and ignore `waiting` while `V.seeking`. `tick()` also carries a watchdog for the stall/resume handshake.
- `web/static/js`: `queue-logic.js` (pure, Node-tested: shuffle bag, reorder, quality pick, sleep, window geometry) · `player.js` (engine + dock + queue; dual mode = muted `<video>` + clock-master `<audio>` with drift correction) · `vwin.js` (floating window). Playback is always started from a user gesture (autoplay policy).
- Tailwind's `.collapse {visibility: collapse}` utility clashes with Bootstrap's `.collapse` → overridden in `app.css`. Never remove that rule.
- On Windows the venv launcher spawns the real server as a child whose command line lacks the project path — when restarting dev processes match `uvicorn api.main:app` / `huey_consumer tasks.huey`, not the path.

---

## §10. Learned pitfalls

1. **yt-dlp needs a JS runtime** (node/deno/bun) for YouTube; `yt-dlp[default]` bundles the solver scripts. Without a runtime some formats are missing.
2. **ffmpeg only, no ffprobe**: the bundled imageio-ffmpeg binary is enough for extraction, merging, tags and cover art.
3. **Mix/radio lists (`list=RD…`)** are "unviewable" as `playlist?list=`; they only load as `watch?v=…&list=RD…` (handled in `normalize_url`). `music.youtube.com` playlist URLs are redirected to youtube.com by yt-dlp — expected.
4. yt-dlp looks for the **pre-conversion** filename (`.webm`) when deciding "already downloaded", so after an mp3 conversion it would redownload — hence our own `_existing_output` check.
5. **Slots are per run, not per item** (`<item>:<run>`): a stale task releasing "its" slot must not free a newer run's slot.
6. A progress write after an item was deleted would resurrect its hash — `patch_item` checks existence and undoes it.
7. `HTTP 403` from googlevideo is usually transient; it is classified retryable and re-queued once with a delay.
8. Huey's `on_startup` fires per worker thread (guarded).
9. `StaticFiles` + heuristic browser caching would serve stale UI after edits — we force revalidation.
10. Sending a stale/zero-byte output as "done" is avoided: empty files are not trusted as existing downloads.
11. Pausing during post-processing (ffmpeg) is not interruptible: pause is ignored (the item completes); cancel deletes the finished output.
12. Playlist numbering width comes from the **full playlist size**, not the selection, so `007 - …` sorts correctly when you pick tracks 7 and 250.
13. Preview panes in a hidden browser tab freeze CSS animations at frame 0 (elements look transparent) — a testing artifact, not a UI bug.
14. **Cookies / "Sign in to confirm you're not a bot"**: `DL_COOKIES_FROM_BROWSER` reads the browser profile and therefore works only when the app runs on the host (not in Docker). In Docker use `DL_COOKIES_FILE=/app/cookies/cookies.txt` (Netscape format) with `./cookies` mounted (read-write: yt-dlp saves the jar back) into `app` and `huey` (`compose.yml`; `cookies/` is gitignored). Both streaming (`services/streaming.py`) and downloads go through `ytdl_opts` so one setting covers both.
15. **Node must be supported by yt-dlp.** Debian's `nodejs` (v20) is reported "unsupported": the n-challenge solver stays unavailable and, once cookies make yt-dlp skip the cookie-less clients, every video fails with "No video formats found". The Dockerfile copies Node 22 from `node:22-slim`. Check with `yt-dlp -v` ("JS runtimes: node-… (unsupported)").
16. **Worker progress logging** is plain lines (`services/downloader.progress_log_due` / `progress_line`, mode `TASK_LOG_PROGRESS`), never tqdm: no TTY in `docker logs`, parallel threads would interleave bars. Each line carries `item_id[:6]`.
17. **Default folder** is a runtime setting (`RuntimeSettings.default_folder`, sanitised by `RuntimeService.clean_folder`, leading dots stripped). `metadata_service.folder_for_single/playlist` apply it; an explicit per-download `folder` always wins. It is relative to the downloads root: browsers cannot expose absolute client paths.
18. **Numbering** is per download (`DownloadOptions.numbering`, request field `numbering`, default `DL_PLAYLIST_NUMBERING`); the runner reads `ctx.options.numbering`, not the setting.
19. **Cover art**: `library_service.cover()` returns a same-stem image or ffmpeg-extracts the embedded picture (JPEG ≤640 px) into `downloads/.covers/` (hidden, keyed by path+mtime+size, 3 ffmpeg at a time). Audio files only. The UI treats a 404 as "no cover" (icon fallback); never loop on `onerror`.
