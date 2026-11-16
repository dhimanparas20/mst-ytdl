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

Output layout: single video → `downloads/<today>/Title.ext`; playlist / cart → `downloads/<Name>/NN - Title.ext`.

Two modes (`SERVER_MODE`, §9c): **personal** (default — everything in this file unless stated) and **server** (public: per-visitor queues, files handed to the browser then deleted, admin-locked management). Personal-mode behaviour must never change because of server-mode code: every server branch is behind `get_settings().server.mode` (or `access_service.server_mode`).

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
| API | `api/` | routers, schemas, error mapping, static/UI mounting; `api/deps.py` = request-scoped dependencies (`Owner`, `admin_only`, `rate(...)`, visitor middleware) | logic — call services |
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
| `DownloadSettings` | `dl_` | `DL_DOWNLOADS_DIR`, `DL_TIMEZONE`, `DL_SPEED_LIMIT` (`2M`), `DL_ALLOWED_HOSTS`, `DL_COOKIES_*`, `DL_DEFAULT_FOLDER`, `DL_PLAYLIST_NUMBERING`, `DL_LYRICS_URL`, `DL_WEBHOOK_URL` |
| `TaskSettings` | `task_` | `TASK_WORKERS`, `TASK_MAX_PARALLEL(_CAP)`, `TASK_RETRY_*`, `TASK_LOG_PROGRESS`, `TASK_IMMEDIATE` |
| `ServerSettings` | `server_` | `SERVER_MODE` (field `mode`), `SERVER_ADMIN_PASSWORD_HASH`, `SERVER_ADMIN_SESSION_HOURS`, `SERVER_DELIVERY_TTL_MINUTES`, `SERVER_MAX_ACTIVE_JOBS`, `SERVER_MAX_JOB_ITEMS`, `SERVER_MAX_SUBSCRIPTIONS` (default 20; personal mode: fixed cap `PERSONAL_MAX` = 100 in `services/subscriptions.py`; also an admin `KNOB`), `SERVER_MIN_FREE_MB`, `SERVER_RATE_LIMIT_PER_MINUTE`, `SERVER_CACHE_MINUTES`, `SERVER_VISITOR_COOKIE_HOURS`, `SERVER_SHARE_SERVER_COOKIES`, `SERVER_MAX_SPEED`, `SERVER_ADMIN_USERNAME`, `SERVER_MAINTENANCE(_MESSAGE)`, `SERVER_ANALYTICS_DAYS`, `SERVER_CONNECT_ENABLED` |
| *(logging, not a settings class)* | – | `LOG_LEVEL`, `LOG_FORMAT`, `LOG_DIR`, `LOG_TO_FILE`, `LOG_ROLE` — read from the environment by `modules/logger.py` |

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
| `metadata.py` | `metadata_service` | URL → allow-list → single/playlist decision; `preview`, `expand_playlist`, `search`, `latest` (uncached head of a channel / playlist + Shorts / not-yet-live detection, for subscriptions); the only place that calls yt-dlp for metadata (`_extract`, `_extract_recent`) |
| `ytdl_opts.py` | functions | Option mapping: `format_selector`, `format_sort`, `build_postprocessors`, `build_ydl_opts`, `friendly_error` |
| `downloader.py` | `download_runner` | Runs ONE item: stale check → wait for slot → atomic claim → yt-dlp with hooks → settle (completed / paused / cancelled / failed / retry) |
| `manager.py` | `download_manager` | Orchestration: `submit`, `checkout_cart`, `resolve_job`, pause/resume/cancel/retry (item, job, bulk), delete, reads, `snapshot`, `job_file` / `item_file`, `reconcile`. Public methods take `owner` (server mode: the visitor; other visitors' records raise `NotFoundError`; `None` = unscoped) |
| `connect.py` | `connect_service` | Extension sign-in: one-time pairing codes `P:CONNECT:<code>` (10 min, single use, bound to the asking visitor), `start` / `status` / `submit`; `submit` refuses cookies without a login session BEFORE saving, then stores them like pasted ones (`save_visitor` / `save`). Switch: `SERVER_CONNECT_ENABLED` (admin knob). The extension itself lives in `extension/` (zip served by `api/routers/connect.py`, address baked in) |
| `explore.py` | `explore_service` | Home Explore (For you / Music / Videos), paginated ≤10 pages: For you via `account_service.home` (login required); Music/Videos via multi-query search pool (anonymous OK, flaky without cookies). UI hides anonymous shelves unless Settings → `exploreAnon` (beta) |
| `account.py` | `account_service` | The signed-in user's own playlists (`/feed/playlists` + pinned `LM`/`LL`/`WL`) and home feed (`:ytrec`, videos only), read through `metadata_service.feed`. `signed_in(owner)`: personal = server cookies carry a login; **server mode = the visitor's own cookies only, never the server's**. Cached per visitor (`cookie_scope_tag`), never raises (failure → `message`) |
| `lyrics.py` | `lyrics_service` | Player lyrics from LRCLIB (`DL_LYRICS_URL`): `clean_title`, exact `/api/get` then `/api/search`, optional `lang` (prefer matching script, else machine-translate line-by-line; `auto` = original), `parse_lrc`, Redis cache `P:LYRICS:<sha1>` incl. lang (hits 7 d, misses 1 d); provider/translate trouble → message, never an exception |
| `cookies.py` | `cookie_service` | Server-mode visitor cookies: `save_visitor` / `visitor_status` / `clear_visitor` (`P:VCOOKIES:<visitor>`, TTL), and `scope(owner)` — the context manager every yt-dlp call of a visitor runs in. Web-UI cookies: `save(text)` validates Netscape text (`modules/cookies_util.py`), keeps only youtube/google domains, atomic 0600 write to `DownloadSettings.cookies_path`; `status()` (counts/flags, **never values**); `test()` = live yt-dlp check of a public video; `clear()`. `ytdl_opts` passes the file to yt-dlp only if it exists (checked per call → no restart); the API clears the stream cache on change |
| `library.py` | `library_service` | Browse/serve/zip/delete under the downloads root; `safe_path` rejects anything escaping it (server mode: also any dot segment, so `.deliveries`/`.tmp` are unreachable); `usage()` = library totals + disk total/used/free (server mode: cached 10 s); `zip_entries()` streams a ZIP of given files |
| `admin_config.py` | `admin_config` | Live settings: a whitelist of `KNOBS` (`section.field`) the admin may change without a restart; overrides in Redis `P:CONFIG` (+ `P:CONFIG:VER`), applied onto the cached `Settings` of every process by a 3 s watcher (API lifespan, Huey `on_startup`); removing one restores the `.env` value. Hard ceilings: `task.max_parallel_cap` (≤ `TASK_WORKERS`) and `server.max_speed` — enforced in `runtime.update`, `manager._server_request` and `ytdl_opts.effective_speed` |
| `analytics.py` | `analytics` | Dashboard statistics, fire-and-forget Redis counters (`P:STAT:D:<day>`, `P:STAT:H:<hour>`, formats, HyperLogLog visitors, top titles / searches, last 200 failures) + the visitor list (`P:VISITORS`, `P:VISITOR:<id>`). `suggest_queries` powers `GET /api/search/suggest` for the omnibar. Recording never raises |
| `files.py` | `file_manager` | Admin file manager over the whole downloads root (hidden entries too): list, mkdir, rename, move / copy / delete (batch), zip, text preview, usage; own `safe_path`; the root itself is immutable |
| `subscriptions.py` | `subscription_service` | Follow a channel / playlist (`create` / `list_for` / `get` / `pause` / `resume` / `delete` / `check` / `poll_all`). Keys: `P:SUB:<id>` (hash, JSON per field, no TTL), `P:SUBS` + `P:OWNER_SUBS:<owner>` (zsets), `P:SUB_SEEN:<id>` (set of video ids), `P:SUB_LOCK:<id>` / `P:SUBS_LOCK` (poll locks with TTL). `create` snapshots `DownloadOptions` and **seeds the seen-set with what exists now — nothing is downloaded at subscribe time**. A poll reads `metadata_service.latest` (channel: newest 15; playlist: whole list), queues unseen ids oldest-first through `download_manager.submit(owner=sub.owner)` (≤25 per poll), claims each id with `SADD` BEFORE submitting and releases it if the submit is refused (limits / maintenance → retried next poll, `last_error="Waiting: …"`). Shorts (`skip_shorts`) are marked seen, live / upcoming entries are not. `poll_all` never raises and is a no-op in server-mode maintenance. Limit: `SERVER_MAX_SUBSCRIPTIONS` per visitor / 100 in personal mode. Server mode scopes by `owner` like jobs (other visitors → `NotFoundError`) |
| `maintenance.py` | `maintenance` | Admin housekeeping actions (cache clears, sweep, expire deliveries, reconcile, clear finished, visitor cookies, stats), `overview()` for the dashboard, log tail |
| `access.py` | `access_service` | Admin login (username + scrypt hash; works in both modes), `require_superadmin` (the /admin API), IP bans (`P:BANS`). Server mode: visitor ids, admin login (scrypt hash from `.env`, session token in Redis `P:ADMIN:<token>`, lockout `P:LOGINFAIL:<ip>`), `require_admin`, per-IP minute rate limits `P:RATE:…`. Every check is a no-op in personal mode |
| `cache.py` | `cache_service` | JSON cache for previews / searches / playlist expansions / account feeds (`P:META:<sha1>`, `json_enabled` = `SERVER_CACHE_MINUTES > 0`, **both modes**; `clear_json()` runs when the server cookies change), and — server mode only (`enabled`) — the media cache `P:MEDIA:<sha1(video, format, options)>` → a finished file other jobs hard-link (or copy) instead of downloading |
| `delivery.py` | `delivery_service` | Server mode: per-job folders `.deliveries/<job>/`, `maybe_build` (file or stored ZIP once the job is terminal; NONE record when nothing finished), records `P:DELIVERY:<job>` + expiry index `P:DELIVERIES`, `list_for_owner` (delivery-index + `Delivery.owner`, soonest expiry first — not only jobs still in the queue), `extend` ("Keep longer": +1 TTL, **once** — WATCH/MULTI on the record's `extended` flag, also moves the `P:DELIVERIES` score), `purge`, `sweep` (expired, missing deliveries, idle paused jobs, orphan dirs) |
| `webhook.py` | `webhook_service` | `DL_WEBHOOK_URL` (both modes): `notify_job(job_id)` re-derives the job status and, when terminal, POSTs JSON `{event: job.<status>, job_id, title, status, kind, url, total_items, completed, failed, cancelled, folder, error, finished_at}` once per finish (`P:WEBHOOK:<job>:<sig>` SET NX); daemon thread, 10 s timeout, never raises. Called from the runner after an item settles, `manager._after_settle`, failed expansion / reconcile timeout. `Title` (RFC 2047) + `Tags` headers make an ntfy topic URL work |
| `dispatch.py` | – | `Dispatcher` protocol + `set_dispatcher/get_dispatcher` (`enqueue_resolve`, `enqueue_item`, `enqueue_delivery`) |
| `errors.py` | – | `NotFoundError` (404) · `ConflictError` (409) · `InvalidRequestError` (422) · `UpstreamError` (502) · `ForbiddenError` (403) · `RateLimitedError` (429) · `UnavailableError` (503) · base `ServiceError` (400) |

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

- `enums.py` – `MediaFormat` (mp3/m4a/opus/flac/wav/mp4/mkv/webm; `.is_audio`, `.lossy_bitrate`), `DownloadStatus`, `ControlAction`, `JobKind` (`single` / `playlist` / `cart`; `.is_multi`), `DeliveryKind` (`file` / `zip` / `none`), `PlaylistMode`, `ResolveState`, quality tuples.
- `subscription.py` – `SubscriptionKind` (`channel` / `playlist`), `SubscriptionRequest` (`OptionFields` + `url` + `skip_shorts`), `Subscription` (record: options snapshot, `enabled`, `owner`, `last_check` / `last_error` / `last_new` / `downloaded`; `seen_count` is derived on read, never stored), `SubscriptionView.of(sub)` (no owner), `SubscriptionCheck`.
- `account.py` – `AccountPlaylist`, `AccountPlaylists`, `AccountFeed`. `paging.py` – `SearchResults` (+ shared page caps). `lyrics.py` – `LyricLine`, `Lyrics` (+ `language` / `requested` / `translated`). `cookies.py` – `CookieStatus` (+ `scope` server/visitor, `deleted_at`), `CookieTestResult`.
- `download.py` – `OptionFields` (options shared by submit bodies), `DownloadOptions` (normalises `1080p`/`320k`, validates quality per format family; `sponsorblock` = request-only flag, default off → `SponsorBlock` + `ModifyChapters` post-processors cut sponsor / selfpromo / interaction before `FFmpegMetadata`, and it is part of the media-cache key), `DownloadRequest`, `CartEntry` / `CartRequest`, `DownloadJob` (incl. `owner`, `deliver_zip`), `DownloadItem` (incl. `token`, `control`, `partials`), `Delivery` (incl. `extended`) / `DeliveryView` / `DeliveryEntry` (one "My downloads" row), `JobView.build(job, items)` (aggregates; `delivery` is attached by the manager), `MediaInfo`/`EntryInfo`, `RuntimeSettings`, `QueueStats`, `derive_job_status`.
- Models are pure data + validation; no redis/yt-dlp/settings imports. Defaults from settings are applied in `DownloadManager.resolve_options`.

---

## §7. Tasks (`tasks/tasks.py`)

One `RedisHuey("mst-ytdl", results=False, immediate=TASK_IMMEDIATE)`. Tasks: `resolve_job_task`, `download_item_task`, `build_delivery_task` (server mode: prepare a finished job's file / ZIP), periodic `poll_subscriptions_task` (every 15 min → `subscription_service.poll_all()`, which calls services directly and never raises), periodic `reconcile_task` (every minute; also publishes the worker heartbeat `P:WORKER:HEARTBEAT` that `/api/health` reads), periodic `sweep_deliveries_task` (every minute; no-op unless server mode).

Conventions: tasks never raise for expected outcomes (failed/paused/cancelled are *recorded*), so Huey never retries on its own — retries are scheduled by the runner (`dispatcher.enqueue_item(..., delay=TASK_RETRY_DELAY)`). `on_startup` runs once per worker thread, so it is guarded by an event.

Run the consumer with threads (`-k thread`); it works on Windows. `uv run python main.py worker` does this.

---

## §8. Run, containers, tests

- `uv run python main.py worker | api | dev`
- `compose.yml`: `redis`, `huey`, `app` (no profiles, so `up`/`down` act on all three; `docker compose up -d redis` for dev); `./downloads` is mounted into both, and so is the source code (`api/ configs/ models/ modules/ services/ tasks/ web/ main.py`, read-only): **code change → `docker compose restart app huey`**; `.env` change → `docker compose up -d` (recreates, no build); only dependency / Dockerfile changes need `--build`. Never mount the repo root over `/app` — it would hide the image's Linux venv (`/app/.venv`). `Dockerfile` installs ffmpeg and copies Node 22 from `node:22-slim` (§10.15).
- `uv run python main.py hash-password` prints `SERVER_ADMIN_PASSWORD_HASH=…` for `.env`.
- Tests: `uv run pytest` (fast; `TASK_IMMEDIATE=true` set in `tests/conftest.py`, fake Redis per test, `tests/fakes.py::FakeYDL` scripted yt-dlp) and `uv run pytest -m live` (real network). Lint: `uv run ruff check .`.
- **Default `pytest` deselects `live`** (`addopts` in `pyproject.toml`).

---

## §9. Web UI (`web/`)

One SPA shell (`index.html`) with **path pages** (same HTML; client router swaps panels). FastAPI serves the shell for `/`, `/download` (alias `/downloads`), and `/library`. Shared chrome: topbar (logo, page nav, omnibar, status menu, music cart, settings), player dock, modals.

| Path | Panel | What it is |
|---|---|---|
| `/` | `#panel-new` | **Home** — Explore shelves on top, then Find & download (Link / Search / Mine); format/Download/Cart sit in a collapsed composer rail |
| `/download` | `#panel-queue` | **Downloads** dashboard — live queue, stats, bulk actions (features unchanged) |
| `/library` | `#panel-library` | **Library** — browse / play / zip / Your downloads (features unchanged) |
| `/admin` | `admin.html` | Separate admin app (not linked from the visitor UI) |

`app.js` `navigate()` + `popstate` drive `body[data-page]`; `go-new` → `/`, `go-queue` → `/download`, `open-folder` → `/library`. Legacy `/#queue` / `/#library` hashes still resolve once. Desktop: top `#page-nav`; mobile: `#bottom-nav`. Omnibar (debounce + history + `/api/search/suggest` + client cache) stays on every page.

| Module | Role |
|---|---|
| `app.js` | boot, **path router** (`/`, `/download`, `/library`), health polling, SSE wiring |
| `omnibar.js` | universal top search/paste; debounce, local history, suggest API, opaque results panel |
| `explore.js` | Home Explore shelves (For you / Music / Videos) with ≤10-page pager; gated by cookies / `exploreAnon` |
| `status.js` | single status menu (mode · live · cookies · health) |
| `theme.js` | accent presets + circular colour wheel (localStorage `mst.theme`) |
| `prefs.js` | browser-local UI prefs (localStorage `mst.ui`: motion, compact queue, auto-save, confirm delete, explore default, **exploreAnon** (beta), Music links, lyrics language, results layout, ambientViz) — never Redis |
| `audiofx.js` | shared Web Audio graph: 10-band EQ + bass/mid/treble + surround → analyser → speakers (`ensureGraph` / `levels` / `getEq`·`setEq`); one `MediaElementSource` per element for stage + ambient + EQ |
| `equalizer.js` | mini-player Equalizer panel (presets, preamp, bass/mids/treble, surround, graphic bands; localStorage `mst.eq`) |
| `ambient.js` | optional page-wide reactive canvas while the mini player is playing (Settings → Look); pauses/clears when music pauses; hidden during stage; off = normal theme |
| `queue.js` | stats tiles + sparkline, toolbar (bulk globals are inline buttons ≥768 px and a "Manage…" dropdown below — the same `data-q` handlers bind both), filters, **keyed in-place job/item DOM updates** (no re-render of the list per snapshot) |
| `connect.js` | "Sign in with Google" panel of the cookies dialog: opens the Google sign-in tab, shows the code, polls `/api/connect/<code>`, fires `cookies-changed` |
| `composer.js` | link / search / **Mine**, preview, playlist picker, submit; **format / quality / options** in Settings (`mst.prefs`); home uses a collapsed `#composer-rail` (summary bar → expand for Settings + Download / Cart; auto-opens when links are ready); Search results paginated ≤10 pages |
| `library.js` | folder browser, player modal, zip/delete |
| `deliveries.js` | server mode "Your downloads" on the Library page (`GET /api/deliveries`): Play + Save + "Keep longer" (once), countdown, urgency pulse |
| `settings.js` | settings modal (admin runtime controls + theme + **user prefs**); no infra System dump; cookies via status menu; health banners personal-mode only |
| `sse.js` | EventSource with backoff and a polling fallback |
| `/admin` (`web/admin.html`, `static/admin/admin.{js,css}`) | the separate admin dashboard; reuses `ui.js` / `util.js` / `app.css` tokens; Chart.js from cdnjs. Never linked from the app |
| `stage.js` | immersive player: scenes + synced lyrics + Video; footer tools mirror the mini player (sleep · speed · EQ · download · queue); lyrics preference persists across expand/collapse |
| `subscriptions.js` | bell button + modal (injects its own markup and `static/css/subscriptions.css`): follow by URL, list, check now / pause / resume / unsubscribe; the composer's playlist preview fires the `subscribe-add` window event (`{url, title}`); options = the composer's current `currentOptions()` snapshot |
| `cart.js` | music cart (localStorage `mst.cart`): add via the `cart-add` window event, modal, checkout → `POST /api/cart/checkout` |
| `api.js`, `ui.js`, `util.js` | fetch wrapper, toasts/confirm/ripple, helpers |

Mode classes: `body.server-mode` hides `.local-only` (folder pickers, overwrite, "open in library") and `.admin-ctl` (library edits, parallel/speed, server cookies — on a server those live only at `/admin`); `.server-only` shows only in server mode. The server enforces all of it — the classes are cosmetic.

Rules: never inject untrusted text with `innerHTML` without `esc()`; a snapshot carries items only for unfinished jobs and single-video jobs (finished playlists are fetched on demand); static assets are served with `Cache-Control: no-cache` (ETag revalidation).


## §9b. Player, streaming, temp dirs (added later — read before touching these)

- `services/streaming.py` (`stream_service`): `info(video_id)` (cached ~20 min, `build_info` is pure) → `QualityOption`s (muxed = one file; otherwise video-only + audio-only), `open_media()` relays ONE ≤8 MiB ranged chunk from googlevideo, `subtitle_vtt()` (timedtext with the active Netscape jar via `active_cookiefile()`, vtt→srt→json3 fallbacks, 429 → `RateLimitedError`). Client never supplies upstream URLs (video id regex + format id must exist in the cached yt-dlp result; hosts limited to googlevideo/youtube).
- **CDN quirks (verified live):** small header ranges (`Range: bytes=0-65535`) 403 on large adaptive streams, and a freshly signed URL is refused for ~1–2 s. So: use the `&range=a-b` query parameter with the exact size from the URL's `clen`; retry 401/403 up to `_ATTEMPTS` with `_RETRY_DELAY_S`; drop the cache on final failure.
- **Timedtext without cookies 429s** (HTML captcha page → was a 502). `subtitle_vtt` loads the same jar yt-dlp would use; without cookies auto-subs often fail silently in the player (toast only on a user-picked track).
- Every download writes into a private `downloads/.tmp/<item_id>/` (yt-dlp `paths.temp`); only the finished file is moved to its folder. This prevents same-title parallel jobs (mp3+mp4+opus) from colliding on thumbnails/intermediates, makes cancel = delete one directory, and resume independent of file names. Dot-names are hidden from the library/zip/usage. The destination folder is NOT pre-created (no empty folders after failures).
- Item `force` (set by *restart*) = ignore an existing output and overwrite; cleared when the run ends, kept across auto-retry.
- **Dual-stream (HD) player loop — never re-seek the picture on every `playing`.** Setting `V.currentTime` makes the video emit `waiting`; the handler then pauses the audio clock, `playing` resumes it, and the cycle repeats ~70×/s with the clock frozen at 0:00 (reproduced with an event log). Re-seek only on real drift (>0.3 s) and ignore `waiting` while `V.seeking`. `tick()` also carries a watchdog for the stall/resume handshake.
- `web/static/js`: `queue-logic.js` (pure, Node-tested: shuffle bag, reorder, quality pick, sleep, window geometry) · `player.js` (engine + dock + queue; dual mode = muted `<video>` + clock-master `<audio>` with drift correction) · `vwin.js` (floating window). Playback is always started from a user gesture (autoplay policy).
- Tailwind's `.collapse {visibility: collapse}` utility clashes with Bootstrap's `.collapse` → overridden in `app.css`. Never remove that rule.
- On Windows the venv launcher spawns the real server as a child whose command line lacks the project path — when restarting dev processes match `uvicorn api.main:app` / `huey_consumer tasks.huey`, not the path.

---

## §9c. Server mode (`SERVER_MODE=true`)

- **Visitors**: `api.deps.visitor_middleware` (registered by `create_app` only in server mode) gives each browser an HttpOnly `ytdl_vid` cookie; routers take `who: Owner` and pass `owner=` to the manager. Jobs carry `owner`; `P:OWNER_JOBS:<owner>` is the per-visitor index next to `P:JOBS`.
- **Where files go**: `delivery_service.folder_for(job_id[, name])` → `.deliveries/<job>` (single) or `.deliveries/<job>/<Name>` (playlist / cart). A job is a server job iff its `folder` starts with `.deliveries` — the sweep never touches anything else, even if the mode is switched later.
- **Delivery**: the runner calls `maybe_build` after the last item settles; direct cancels go through `dispatcher.enqueue_delivery`; the sweep builds any that are missing. `GET /api/downloads/{id}/file` serves it (409 while preparing).
- **Admin**: the dashboard is `/admin` (`api/routers/admin.py`: `router` for status / sign-in, `guarded` for everything else behind `superadmin`). The app's own write endpoints (library writes, cookie writes, `PUT /runtime`) keep `admin_only` (server mode: an admin session; personal mode: open). The visitor middleware also refuses banned IPs (not the admin area) and records visitors for the dashboard.
- **Cookies**: every yt-dlp call for a visitor runs inside `cookie_service.scope(owner)`; `ytdl_opts._apply_cookies` reads the scope from a `ContextVar` (signatures unchanged). No scope = the server's cookies (personal mode, admin tools). Caches key on `cookie_scope_tag()` ('' anonymous → shared). The media-file cache stays shared (identical output).
- **My downloads / Keep longer**: `GET /api/deliveries` (`download_manager.list_deliveries`: only ready, unexpired, still-on-disk files of the caller; `[]` in personal mode) and `POST /api/deliveries/{job}/extend` (owner-checked via the job; adds one `SERVER_DELIVERY_TTL_MINUTES`, 409 the second time, 404 when gone, 422 in personal mode). Urgency lives in `queue-logic.js::deliverySoon` (< 10 min → amber expiry text + pulsing Save button, on the job card and in the list).
- **Maintenance**: `GET /api/config` reports `maintenance` / `maintenance_message` (server mode only); `app.js` shows a banner and re-checks every 60 s. The refusal itself is `manager._server_request` (HTTP 503) — the banner is only the heads-up.
- **Disabled for visitors**: `folder` and `overwrite` are dropped from requests; limits per visitor / job / disk / IP (see §3).

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
11. Pausing during post-processing (ffmpeg) is not interruptible: pause is ignored (the item completes); cancel deletes the finished output. The UI hides Pause on `processing` and shows *Finishing conversion…* if a pause was already requested.
12. Playlist numbering width comes from the **full playlist size**, not the selection, so `007 - …` sorts correctly when you pick tracks 7 and 250.
13. Preview panes in a hidden browser tab freeze CSS animations at frame 0 (elements look transparent) — a testing artifact, not a UI bug.
14. **Cookies / "Sign in to confirm you're not a bot"**: `DL_COOKIES_FROM_BROWSER` reads the browser profile and therefore works only when the app runs on the host (not in Docker). In Docker use `DL_COOKIES_FILE=/app/cookies/cookies.txt` (Netscape format) with `./cookies` mounted (read-write: yt-dlp saves the jar back) into `app` and `huey` (`compose.yml`; `cookies/` is gitignored). Both streaming (`services/streaming.py`) and downloads go through `ytdl_opts` so one setting covers both. YouTube rotates open-browser sessions: lasting exports need **Allow in Incognito** on the exporter / connect extension, sign-in in Incognito, land on `youtube.com/robots.txt`, then close that window. The connect flow's Google continue URL is that robots.txt page (not the homepage).
15. **`/api/health` is `degraded` without a JS runtime** (same as missing ffmpeg/worker). **Node must be supported by yt-dlp.** Debian's `nodejs` (v20) is reported "unsupported": the n-challenge solver stays unavailable and, once cookies make yt-dlp skip the cookie-less clients, every video fails with "No video formats found". The Dockerfile copies Node 22 from `node:22-slim`. Check with `yt-dlp -v` ("JS runtimes: node-… (unsupported)").
16. **Worker progress logging** is plain lines (`services/downloader.progress_log_due` / `progress_line`, mode `TASK_LOG_PROGRESS`), never tqdm: no TTY in `docker logs`, parallel threads would interleave bars. Each line carries `item_id[:6]`.
17. **Default folder** is a runtime setting (`RuntimeSettings.default_folder`, sanitised by `RuntimeService.clean_folder`, leading dots stripped). `metadata_service.folder_for_single/playlist` apply it; an explicit per-download `folder` always wins. It is relative to the downloads root: browsers cannot expose absolute client paths.
18. **Numbering** is per download (`DownloadOptions.numbering`, request field `numbering`, default `DL_PLAYLIST_NUMBERING`); the runner reads `ctx.options.numbering`, not the setting.
19. **Cover art**: `library_service.cover()` returns a same-stem image or ffmpeg-extracts the embedded picture (JPEG ≤640 px) into `downloads/.covers/` (hidden, keyed by path+mtime+size, 3 ffmpeg at a time). Audio files only. The UI treats a 404 as "no cover" (icon fallback); never loop on `onerror`.
20. **All log files live in one directory** (`logs/`, `LOG_DIR`): `<role>.log`, `<role>.errors.log` (WARNING+), `access.log`. `get_logger` adds shared `RotatingFileHandler`s; uvicorn/huey reset their own handlers at start-up, so `attach_server_logs()` is called from the API lifespan and the Huey `on_startup` hook. `LOG_ROLE` comes from `main.py` / `compose.yml`. Never log to a path outside `logs/`; tests set `LOG_TO_FILE=false` (`tests/conftest.py`). Rotation is per process, so two roles never share a file. Successful ``GET /api/health`` access lines are filtered out (`QuietHealthAccessFilter` / `quiet_health_access()` at API import) — the UI polls them every few seconds and they would drown the console.
21. **Never use a `YoutubeDL` after its `with` block.** Leaving the block closes its HTTP session; touching `ydl` again silently builds a new session that nothing closes, so every download leaked its YouTube connections (CLOSE_WAIT) until `OSError: [Errno 24] Too many open files` (soft limit 1024) — it once happened because a log line was inserted at the wrong indentation and ended the `with` early. Keep `extract_info`, `prepare_filename` and `process_ie_result` inside it. `tests/fakes.py::FakeYDL` now fails on use-after-close, and `TestSessionLifecycle` covers every `TASK_LOG_PROGRESS` mode. Containers also get `ulimits: nofile: 65536` as a safety net. To diagnose: count `/proc/<pid>/fd` of the worker and group sockets by remote address/state.
22. **Server-mode file layout must stay per job.** Two visitors downloading the same title at the same time would otherwise write (and the sweep delete) the same file. Never put a server job's output anywhere but `.deliveries/<job_id>/`; reuse goes through the media cache (hard link / copy into the job's own folder).
23. **The admin hash uses `:` separators, not `$`.** Docker Compose may interpolate `$` in env files and would silently mangle a `$`-style hash.
24. **`Content-Disposition` for user-named files must be RFC 5987** (`api.deps.attachment`): a playlist / cart name with non-Latin-1 characters crashes a plain `filename="…"` header.
25. **Auto-save in the UI only fires for jobs this tab saw finish** (`queue.js::maybeAutoSave`, tracked across reloads in `sessionStorage`), otherwise every page load would re-download every finished job. The auto-save first probes the file URL with a 1-byte Range request (server mode) so a refusal (expired / not ready / offline) becomes a warning toast instead of a silent no-op; a *blocked* anchor download cannot be detected, which is why the Save button also pulses when the file is about to expire.
26. **A media element routed through a suspended `AudioContext` goes silent.** `audiofx.js::ensureGraph` (stage / ambient / equalizer) wires `<audio>`/`<video>` only after `resume()` reports `running` (and only from a user gesture); otherwise the scenes use a simulated pulse. An element can be wired once — afterwards its sound always flows through the EQ → surround → analyser graph, so the context is resumed on visibility changes. Do not create a second `MediaElementSource` for the same element.
27. **Hidden preview panes pause `requestAnimationFrame`**, not just CSS animations (#13): the stage's render loop (clock, lyric highlight, visuals) does not tick there. To test it headless, replace `requestAnimationFrame` with a timer before opening the stage.
28. **Run `uv` from Windows, not WSL, in this checkout.** `.venv` is a Windows venv (`.venv/Lib`); WSL's `uv` decides to rebuild it as a Linux venv and deletes packages under a running app (seen: `annotated_types` / `charset_normalizer` vanished, yt-dlp failed with `FileNotFoundError` in its HTTP layer). Repair with `uv sync --reinstall` from Windows.
29. **Tests pin `SERVER_MODE=false`** (`tests/conftest.py`): a developer `.env` with `SERVER_MODE=true` otherwise leaks into every test (47 failures the first time).
30. **The parallel ceiling is the worker threads.** `TASK_MAX_PARALLEL_CAP` (default 12) can exceed `TASK_WORKERS` (6), which made the admin panel show "12 · max 6". Server mode reports and enforces `min(cap, threads)` (`RuntimeService.parallel_cap`); personal mode keeps the old clamp so nothing changes there.
31. **Account feeds need valid login cookies, and music.youtube.com has no home extractor.** yt-dlp reads `/feed/playlists`, `LL` / `WL` / `LM` and `:ytrec` only with a live session ("cookies are no longer valid" = rotated in the browser; re-export). The Music home page is unsupported, so the Mine tab shows Liked music + the YouTube home feed. In server mode never fall back to the server's cookies for these views: it would show the owner's account to every visitor (`AccountService.signed_in` checks `visitor_status`).
32. **No Google sign-in popup exists for this.** A page cannot read another origin's cookies, and OAuth would need the operator's own Google Cloud client; so account linking is a pairing code + the `extension/` (which holds the `cookies` permission). Never add a server-side "type your Google password here" browser: it would make the server a credential collector. The pairing code is the only credential of `POST /api/connect/<code>`, so keep it single use, short-lived and rate-limited, and keep the login check before the save.
33. **Cookies dialog target depends on the mode, and `settings.js` only learns the mode once the config is loaded.** Anything that opens the dialog from elsewhere goes through the `open-cookies` event (it loads the config first); server mode edits `mine`, never the admin-only server cookies.
34. **The JSON meta cache runs in personal mode too** (`SERVER_CACHE_MINUTES`, default 10; 0 = off), so a preview / search / playlist read can be up to that old. Personal mode has a single cache scope (`cookie_scope_tag()` is `''`), so saving / clearing the server cookies or finishing the extension sign-in calls `cache_service.clear_json()` — otherwise a signed-out answer would outlive the new cookies. The media-file cache stays server-mode only.

35. **Subscriptions must never back-fill and never double-queue.** Subscribing seeds `P:SUB_SEEN:<id>` with the current head instead of downloading it (a channel's whole history would otherwise land in the queue). Each id is `SADD`-claimed *before* `submit` (a manual check can race the periodic poll; the per-subscription lock is only an optimisation) and `SREM`-released when the submit is refused. A poll that finds the subscription deleted removes the seen-set it may have recreated (`_patch` refuses to resurrect a deleted record). Playlists are read in full, channels only the newest 15: new playlist entries are usually appended, a channel lists newest first, so the enqueue order is reversed only for channels.
36. **The webhook is announced per *finish*, not per job.** The dedupe key hashes status + the latest `finished_at`, so concurrent last items and repeated `_after_settle` calls send one POST, while a retry that completes later sends another. It uses stdlib `urllib` (httpx is a dev-only dependency) on a daemon thread; tests monkeypatch `webhook_service._dispatch`.
37. **YouTube timedtext needs cookies.** A bare `GET` to the subtitle URL from yt-dlp's info dict returns 429 (HTML) without a login jar; with Netscape cookies it returns WEBVTT. Media streaming can work anonymously; `/api/stream/.../subs` sends the active jar, and if the visitor has none falls back to the server jar (public captions only — not account feeds). Convert srt/json3 when vtt is missing or refused.
