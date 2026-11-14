# mst-ytdl

A self-hosted **YouTube & YouTube Music downloader**: FastAPI + Huey (Redis) + yt-dlp, with a dark, glass-morphism web UI.

Paste a video, a song, an album or a whole playlist → pick MP3 / MP4 (and more) and a quality → watch it download live, with **pause / resume / cancel / retry**, parallel workers and per-item status.

```
downloads/
├── 2026-11-14/                  ← single videos: folder named after today's date
│   ├── Me at the zoo.mp3
│   └── Me at the zoo.mp4
└── My Road Trip Mix/            ← playlists: folder named after the playlist
    ├── 01 - First song.mp3
    └── 02 - Second song.mp3
```

## Features

| Area | What you get |
|---|---|
| Sources | YouTube + YouTube Music: videos, shorts, `youtu.be`, playlists, albums, channels, Mix/radio lists; **text search**; many links at once |
| Formats | Audio **MP3** (64–320 kbps), M4A, Opus, FLAC, WAV · Video **MP4** (144p–4K), MKV, WebM |
| Quality | Bitrate / resolution chooser; the UI greys out sizes a video doesn't offer |
| Control | **Start, pause, resume, cancel, retry** per track, per job, or for the whole queue. Pause keeps the `.part` file, so resume continues from the same byte |
| Parallelism | Huey consumer threads = hard ceiling; a **live "parallel downloads" limit** (UI stepper) enforced across processes by a Redis slot limiter, plus yt-dlp's parallel fragment download inside each file |
| Status | Per-job and per-track progress, speed, ETA, bytes, stage ("merging", "converting audio"…), live over **SSE** with a sparkline of total speed |
| Extras | Cover art + tags embedded, subtitles (download + embed), speed cap, skip files that already exist (or *overwrite*), playlist track picker (`1-5, 8, 12-`), custom folder name, ZIP download of a playlist, built-in player + library browser |
| Resilience | Auto-retry for transient errors (not for private/removed videos), crash recovery (dead workers' downloads become *Paused*), health pill (Redis / worker / ffmpeg) |
| Safety | Host allow-list (YouTube only), path-traversal-proof library API, loopback bind by default |

## In-app player (play anything, no downloading needed)

A docked player plus a **floating video window** (drag by the header, resize from any corner, snap to edges, S/M/L presets, fullscreen, picture-in-picture; position is remembered; docks above the player on phones).

- **Plays every YouTube video yt-dlp can read** — streams are relayed by *our* server (`/api/stream/<id>/…`, Range-capable), not YouTube's iframe, so "owner disabled embedding" never blocks you. Above YouTube's muxed 720p the picture and sound are separate streams, played as two clock-synced elements.
- **Quality** (Auto / 1080p / 720p / … / Audio only), **subtitles** (manual + auto captions, size S/M/L; also for downloaded videos: embedded tracks via ffmpeg + `.srt/.vtt` sidecars), **speed** 0.5–2×, volume, seek with buffered range + hover time, repeat (off/all/one), **true shuffle** (every track once per cycle), reorderable queue (drag), *shuffle rest*, **sleep timer** (15–60 min or end of track, fades out), media-keys/OS controls.
- Play/Shuffle buttons on: search results, playlist previews (before downloading), finished playlist jobs, library folders and selections.
- Keys: `Space/K` play · `←/→` ±5 s · `J/L` ±10 s · `↑/↓` volume · `M` mute · `F` fullscreen · `C` cycle captions · `T` video window · `N/P` next/prev · `S` shuffle · `R` repeat · `Q` queue · `< >` speed · `0–9` jump to %.

## Logs

Everything is written under one directory, **`./logs/`** (gitignored; in Docker `./logs` is mounted into `app` and `huey`):

| File | Contents |
|---|---|
| `api.log` | API process: app code, uvicorn |
| `worker.log` | Huey worker: downloads, progress lines, retries, huey itself |
| `api.errors.log`, `worker.errors.log` | `WARNING` and above only |
| `access.log` | One line per HTTP request |

Each file rotates at 5 MB (5 backups). `LOG_ROLE` (`api` / `worker`, set by `main.py` and compose) names a process' files; plain `uvicorn …` without it writes `app.log`. Passwords, tokens and cookies are masked. The console output is unchanged (`docker compose logs -f huey`).

## Smaller things worth knowing

- **Worker log lines** (`docker compose logs -f huey`): `[a1b2c3] Believer  40%  3.1 MiB/s  eta 0:12`, plus start / converting / completed (size + time) / failed. Plain lines, not tqdm: progress bars need a TTY and several parallel downloads would garble each other.
- **Speed tile**: shows the live speed plus **average** (only while downloading) and **peak** for this browser session (↺ resets).
- **Default download folder**: Settings → *Default download folder* (Browse lists existing folders or type a new one). Singles go straight into it, playlists into `<folder>/<Playlist name>`; a folder typed in the composer still wins. A browser cannot open a native file explorer for a server path; move the whole root with `DL_DOWNLOADS_DIR` (Docker: the compose volume).
- **Filename numbering**: Options → *Number playlist tracks*. Off = `Title.mp3` instead of `01 - Title.mp3`. The server default is `DL_PLAYLIST_NUMBERING`.
- **Album art in the player**: covers embedded in your files (and `Title.jpg` next to them) are served by `GET /api/library/cover` and shown in the dock, queue and library list.

## Download management

Failed / paused / cancelled / finished downloads each have their own filter and a contextual bar (*Retry all · Restart all · Remove all* …). Per item: **Retry** (continue from the partial file), **Restart / replace** (fresh download that overwrites the old file only once it succeeds), **Remove**. **Select** mode gives a multi-select bar (pause, resume, retry, restart, cancel, remove, remove + files). Library: multi-select → play, queue, ZIP, **move to folder**, delete; rename files/folders. Custom destination folder per download (nested like `Music/Rock`, suggestions from existing folders).

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/), Docker (for Redis), Node.js (yt-dlp uses a JS runtime to solve YouTube's challenges; Deno/Bun also work). ffmpeg ships with the project (`imageio-ffmpeg`) unless you have your own.

```bash
uv sync
cp .env.sample .env                 # adjust if you like (see the table below)
docker compose up -d redis          # Redis on localhost:6379

uv run python main.py dev           # worker + API (reload) together
# …or in two terminals:
uv run python main.py worker        # Huey consumer — does the downloading
uv run python main.py api           # FastAPI + UI on http://127.0.0.1:8000
```

Open <http://127.0.0.1:8000>. API docs: <http://127.0.0.1:8000/docs>.

> **Windows + WSL Docker:** run compose through WSL, e.g. `wsl -e sh -c "cd /mnt/d/0Coding/mst-ytdl && docker compose up -d redis"`. Redis is reachable from Windows on `localhost:6379`.

Everything in containers: `docker compose up -d --build` (Redis + worker + API, downloads mounted at `./downloads`); `docker compose down` stops it all. For local dev with `uv run`, start only Redis: `docker compose up -d redis`.

## Cookies (fixes "Sign in to confirm you're not a bot")

YouTube sometimes blocks an IP that makes many requests; every lookup (downloads **and** the player) then fails with that message. Give yt-dlp your logged-in browser's cookies:

| Setting | Works where | How |
|---|---|---|
| `DL_COOKIES_FROM_BROWSER=chrome` (or `firefox`, `edge`) | Only when the app runs **on your machine** (`uv run`), **not in Docker** (the container cannot see your browser profile) | Set it in `.env`, restart |
| `DL_COOKIES_FILE=<path to an exported cookies.txt>` | Everywhere, **including Docker** if the file is mounted into the container | see below |

**Easiest — in the web UI:** Settings → *YouTube cookies* → **Edit**, paste the Netscape `cookies.txt`, **Save cookies**. The server validates the format, keeps only youtube.com / google.com cookies, stores them (owner-only) in `cookies/cookies.txt` and immediately runs a **live test** against YouTube (shows ✓/✗ with the reason). Settings shows whether cookies are loaded, whether they are a signed-in session, and when the login expires (warns when logged-out, expired or expiring within a week). Cookie values are never sent back to the browser. *Test* re-checks any time; *Remove cookies* deletes the file. No restart needed.

**Docker (manual file):** 1) In a browser where you are logged in to YouTube, install a "cookies.txt" exporter extension (e.g. *Get cookies.txt LOCALLY*), open youtube.com and export in **Netscape format**. 2) Save it as `./cookies/cookies.txt` (compose mounts `./cookies` at `/app/cookies`; the folder is gitignored). 3) In `.env` set `DL_COOKIES_FILE=/app/cookies/cookies.txt`. 4) `docker compose up -d --build`. Tip: use a throw-away Google account — heavy automated use can get an account flagged — and re-export when cookies expire. Never commit or share the file.

## Architecture

```
 browser ──HTTP/SSE──▶ api/ (FastAPI)  ──▶ services/ ──▶ Redis  (job/item hashes, slots, runtime settings)
                           │                  ▲  │
                           └── enqueue ──▶ tasks/ (Huey) ──▶ services/downloader ──▶ yt-dlp + ffmpeg ──▶ downloads/
```

- **api/** – thin routers; services raise `NotFoundError`/`ConflictError`/`InvalidRequestError`, mapped to 404/409/422.
- **tasks/** – three thin Huey tasks (`resolve_job`, `download_item`, periodic `reconcile`). Services never import tasks: `tasks` registers a dispatcher in `services.dispatch`.
- **services/** – all logic: `manager` (orchestration), `downloader` (runs one item), `store` (Redis persistence, atomic transitions), `runtime` (live settings + slot limiter), `metadata` (URL → preview/playlist/search), `ytdl_opts` (options → yt-dlp), `library` (files).
- **models/** – pydantic requests, records and views. **configs/** – settings + client singletons. **modules/** – generic helpers.

How pause/cancel work: the API writes `control=pause|cancel` on the item's Redis hash; the running worker's progress hook (≈2×/s) sees it and raises `DownloadInterrupted` (a subclass of yt-dlp's own `DownloadCancelled`). Pause keeps the partial file; cancel deletes it. Queued items are changed immediately and their pending task goes stale via a per-run token.

## Configuration (`.env`)

Loaded by `configs/config.py` (real env vars win over `.env`, which wins over defaults). See `.env.sample` for comments.

| Variable | Default | Meaning |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Broker + state store |
| `REDIS_KEY_PREFIX` | `YTDL` | Prefix for every key written |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | Bind address (the app fetches URLs for the caller — don't expose it unauthenticated) |
| `API_DEBUG`, `API_CORS_ORIGINS` | `false`, `["*"]` | |
| `DL_DOWNLOADS_DIR` | `downloads` | Relative to the repo root |
| `DL_TIMEZONE`, `DL_DATE_FOLDER_FORMAT` | `Asia/Kolkata`, `%Y-%m-%d` | Name of the per-day folder for single videos |
| `DL_PLAYLIST_NUMBERING` | `true` | `01 - Title.mp3` inside playlist folders |
| `DL_INCLUDE_ID_IN_NAME` | `false` | Append `[video_id]` to names |
| `DL_MAX_PLAYLIST_ITEMS` | `500` | Cap when expanding playlists |
| `DL_DEFAULT_FORMAT`, `DL_DEFAULT_AUDIO_QUALITY`, `DL_DEFAULT_VIDEO_QUALITY` | `mp3`, `192`, `1080` | UI/API defaults |
| `DL_CONCURRENT_FRAGMENTS` | `4` | Parallel segments inside one download |
| `DL_NETWORK_RETRIES` | `5` | yt-dlp's own retries |
| `DL_SPEED_LIMIT` | – | e.g. `2M` per download |
| `DL_EMBED_METADATA`, `DL_EMBED_THUMBNAIL` | `true` | Tags / cover art defaults |
| `DL_FFMPEG_PATH` | – | Empty = PATH, then bundled binary |
| `DL_JS_RUNTIME` | `auto` | `auto` \| `deno` \| `node` \| `bun` \| `quickjs` |
| `DL_COOKIES_FILE`, `DL_COOKIES_FROM_BROWSER`, `DL_PROXY` | – | Age-gated / member content, proxies |
| `DL_ALLOWED_HOSTS` | YouTube hosts | URL allow-list |
| `TASK_WORKERS` | `6` | Consumer threads (hard parallel ceiling) |
| `TASK_LOG_PROGRESS` | `milestones` | Worker-log progress lines (`docker logs`): `off` · `milestones` (every 10 % / 10 s) · `verbose` (5 % / 5 s) |
| `DL_DEFAULT_FOLDER` | – | Base folder inside `downloads/` for new downloads (also settable live in Settings) |
| `DL_PLAYLIST_NUMBERING` | `true` | Default for the “Number playlist tracks” switch (`01 - Title`); off = plain `Title` |
| `TASK_MAX_PARALLEL` / `TASK_MAX_PARALLEL_CAP` | `3` / `12` | Live parallel limit default / its maximum |
| `TASK_RETRY_COUNT` / `TASK_RETRY_DELAY` | `1` / `10` | Auto-retries for transient failures |
| `TASK_RECORD_RETENTION_DAYS` | `30` | Redis records expire this long after their last change |
| `TASK_IMMEDIATE` | `false` | Run tasks inline (tests only) |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, `text` | `text` or `json` |
| `LOG_DIR`, `LOG_TO_FILE` | `./logs`, `true` | Where the log files go; set `LOG_TO_FILE=false` for console only |

## HTTP API (prefix `/api`)

| Method & path | Purpose |
|---|---|
| `POST /downloads` | Queue URLs `{urls[], format?, quality?, playlist_mode?, playlist_items?, embed_metadata?, embed_thumbnail?, subtitles?, subtitle_langs?, speed_limit?, overwrite?, folder?}` → `{jobs[], rejected[]}` |
| `GET /downloads` · `GET /downloads/{id}` | List (`status`, `limit`, `offset`, `include_items`) / one job with items |
| `POST /downloads/{id}/{pause\|resume\|cancel\|retry}` · `DELETE /downloads/{id}?delete_files=` | Job-level control / removal |
| `GET /items/{id}` · `POST /items/{id}/{pause\|resume\|cancel\|retry}` | Single track |
| `POST /queue/{pause-all\|resume-all\|cancel-all\|retry-failed\|clear-finished}` | Bulk |
| `POST /info` · `GET /search?q=` | Preview a URL (title, qualities, playlist entries) / text search |
| `POST /downloads/bulk` · `POST /downloads/{id}/restart` · `POST /items/{id}/restart` | Multi-select actions (`pause\|resume\|cancel\|retry\|restart\|remove\|remove-files`) / replace |
| `GET /stream/{id}/info` · `/stream/{id}/media?format=` · `/stream/{id}/subs?lang=` | Player: qualities + subtitle tracks, ranged stream proxy, WebVTT |
| `GET /library/cover?path=` | Album art of a downloaded audio file (embedded cover or `.jpg/.png/.webp` sidecar; 404 if none) |
| `GET /library/subtitles` · `/library/subtitle` | Subtitle tracks of a downloaded video |
| `POST /library/bulk-delete` · `/library/move` · `/library/rename` · `GET /library/folders` | Library bulk operations; `GET /library/zip?path=a&path=b` zips a selection |
| `GET /cookies` · `PUT /cookies` `{text}` · `DELETE /cookies` · `POST /cookies/test` | Cookie status (flags/counts only), save pasted Netscape cookies, remove, live-test against YouTube |
| `GET /events` | SSE stream of queue snapshots |
| `GET /library` · `/library/file` · `/library/zip` · `DELETE /library` · `GET /library/usage` | Browse, stream (Range), zip, delete |
| `GET/PUT /runtime` · `GET /config` · `GET /health` | Live settings, UI option lists, dependency check |

## Tests

```bash
node --test "web/tests/*.test.js"   # 26 tests: formatting, speed units, shuffle/queue/quality/window logic
uv run pytest                 # ~920 fast tests (fake Redis + scripted yt-dlp), ~30 s
uv run pytest -m live         # real YouTube / yt-dlp / ffmpeg (downloads a few MB, needs internet)
uv run ruff check .
```

The unit suite covers every module, the pause/resume/cancel/retry state machine (including races), the parallel limiter, path safety, every endpoint and the SSE stream. The live suite downloads real media and checks it (ID3 tags + cover art, 360p cap, decodes cleanly after pause→resume, cancel leaves no files, playlist numbering, zip).

## Troubleshooting

- **Jobs stay "Queued"** – no worker is running (the header pill says *No worker*): `uv run python main.py worker`.
- **"Redis down"** – `docker compose up -d redis`.
- **`HTTP 403` / "Sign in to confirm you're not a bot"** – YouTube is throttling this IP; transient 403s retry automatically. For gated videos set `DL_COOKIES_FROM_BROWSER=chrome` (or `DL_COOKIES_FILE`).
- **YouTube changed something and downloads fail** – update yt-dlp: `uv lock --upgrade-package yt-dlp && uv sync`.
- **Conversions fail** – ffmpeg missing: set `DL_FFMPEG_PATH`.
- Only the first `DL_MAX_PLAYLIST_ITEMS` entries of huge playlists are listed.
