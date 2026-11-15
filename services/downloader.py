"""
DownloadRunner — executes ONE item inside a Huey worker thread.

Lifecycle of ``run(item_id, token)``:

    1. stale-check      item gone / re-queued under another token → exit quietly
    2. wait for slot    SlotLimiter enforces the runtime parallel limit
    3. claim            QUEUED → DOWNLOADING (atomic; one winner per token)
    4. download         yt-dlp, with progress / postprocessor / post hooks
    5. settle           COMPLETED | PAUSED | CANCELLED | FAILED | re-queued retry

Pause / cancel are cooperative: the API writes ``control`` on the item hash and
the progress hook (throttled to ~2×/s) notices it and raises
``DownloadInterrupted`` — a subclass of yt-dlp's own ``DownloadCancelled`` so it
unwinds through yt-dlp untouched. A PAUSE keeps the ``.part`` files, so a later
resume continues from the byte offset; a CANCEL deletes them.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled

from configs import get_ffmpeg_path, get_settings
from models import ControlAction, DownloadItem, DownloadJob, DownloadStatus, JobKind
from modules import get_logger, human_bytes, human_duration, new_id
from modules.ytdlp_util import remove_partial_files
from services.analytics import analytics
from services.cache import cache_service
from services.cookies import cookie_service
from services.delivery import delivery_service
from services.dispatch import get_dispatcher
from services.runtime import runtime_service, slot_limiter
from services.store import job_store
from services.ytdl_opts import build_ydl_opts, friendly_error

logger = get_logger(__name__)

_RUNNING = (DownloadStatus.DOWNLOADING, DownloadStatus.PROCESSING)
_PP_LABELS = {
    "FFmpegExtractAudio": "converting audio",
    "FFmpegMerger": "merging video + audio",
    "Merger": "merging video + audio",
    "FFmpegMetadata": "writing tags",
    "EmbedThumbnail": "embedding cover art",
    "FFmpegEmbedSubtitle": "embedding subtitles",
    "FFmpegSubtitlesConvertor": "converting subtitles",
    "FFmpegVideoRemuxer": "remuxing",
    "FFmpegVideoConvertor": "converting video",
    "MoveFiles": "finalizing",
}
_VIDEO_WEIGHT = 0.9 # share of a video+audio job's progress carried by the video stream
_CHECK_INTERVAL = 0.5  # seconds between control checks / progress writes
#: TASK_LOG_PROGRESS → (percent step, max seconds between lines)
_LOG_STEPS = {"milestones": (10.0, 10.0), "verbose": (5.0, 5.0)}


def progress_log_due(mode: str, pct: float, last_pct: float, now: float, last_t: float) -> bool:
    """
    Should a progress line be logged now? (pure; ``mode`` is ``TASK_LOG_PROGRESS``)

    Args:
        mode: ``off`` | ``milestones`` | ``verbose``.
        pct: current overall percent (0..100).
        last_pct: percent at the previous line (negative = none yet).
        now / last_t: monotonic seconds now / at the previous line.
    """
    step = _LOG_STEPS.get(mode)
    if step is None:
        return False
    pct_step, max_gap = step
    return last_pct < 0 or pct - last_pct >= pct_step or now - last_t >= max_gap


def progress_line(tag: str, title: str, pct: float, speed: float | None, eta: float | None, stage: str) -> str:
    """One worker-log line: ``[a1b2c3] Title  40%  3.1 MB/s  eta 12s  (video)``."""
    bits = [f"[{tag}] {title[:60] or '…'}", f"{pct:3.0f}%"]
    if speed:
        bits.append(f"{human_bytes(speed)}/s")
    if eta:
        bits.append(f"eta {human_duration(eta)}")
    if stage not in ("downloading", ""):
        bits.append(f"({stage})")
    return "  ".join(bits)
_HEARTBEAT_INTERVAL = 10.0


class DownloadInterrupted(DownloadCancelled):
    """Raised from a hook to unwind yt-dlp when the user paused / cancelled."""

    def __init__(self, action: str):
        super().__init__(f"interrupted: {action}")
        self.action = action


class StaleTask(Exception):
    """The item was re-queued or replaced; this task must leave it alone."""


class _Ctx:
    """Mutable per-run state shared by the hook closures."""

    def __init__(self, item: DownloadItem, job: DownloadJob):
        self.item = item
        self.job = job
        self.token = item.token
        # "Restart / replace" forces a fresh download over any existing file
        self.options = job.options.model_copy(update={"overwrite": True}) if item.force else job.options
        self.tmp_dir: Path | None = None
        self.ydl: YoutubeDL | None = None
        self.info: dict[str, Any] = {}
        self.final: str | None = None
        self.seen: set[str] = set()
        self.streams_done = 0
        self.n_streams: int | None = None
        self.bytes_prev = 0
        self.last_check = 0.0
        self.last_write = 0.0
        self.started = time.monotonic()
        self.last_log_pct = -1.0
        self.last_log_t = 0.0
        self.cache_hit = False  # server mode: the file came from the media cache
        self.result: Any = None  # yt-dlp's process_ie_result() return value

    @property
    def tag(self) -> str:
        """Short id for log lines (parallel downloads interleave in one log)."""
        return self.item.item_id[:6]


class _Heartbeat(threading.Thread):
    """Keeps the slot + item fresh during phases without progress events
    (extraction, long ffmpeg runs) so the reconciler never mistakes them for dead."""

    def __init__(self, item_id: str, holder: str, interval: float = _HEARTBEAT_INTERVAL):
        super().__init__(daemon=True, name=f"hb-{item_id}")
        self.item_id = item_id
        self.holder = holder
        self.interval = interval
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.wait(self.interval):
            try:
                slot_limiter.heartbeat(self.holder)
                job_store.patch_item(self.item_id)  # touches updated_at only
            except Exception:  # never let the heartbeat kill a download
                logger.debug("heartbeat failed", exc_info=True)

    def stop(self) -> None:
        self._stop_evt.set()


class DownloadRunner:
    """Runs one item to a settled state. Singleton: ``download_runner``."""

    def __init__(
        self,
        ydl_factory: Callable[[dict], Any] = YoutubeDL,
        slot_poll: float = 1.0,
        heartbeat_interval: float = _HEARTBEAT_INTERVAL,
    ):
        self.ydl_factory = ydl_factory
        self.slot_poll = slot_poll
        self.heartbeat_interval = heartbeat_interval

    # ── entry point ─────────────────────────────────────────────────

    def run(self, item_id: str, token: str) -> str:
        """
        Execute the item and return the outcome:
        ``completed | paused | cancelled | failed | retry | stale | removed``.
        """
        item = job_store.get_item(item_id)
        if item is None or item.token != token or item.status is not DownloadStatus.QUEUED:
            logger.info(f"skip stale task item={item_id}")
            return "stale"
        job = job_store.get_job(item.job_id)
        if job is None:
            return "stale"

        # One holder per execution: a stale/duplicate task must never be able
        # to release the slot of a newer run of the same item.
        holder = f"{item_id}:{new_id()}"
        if not self._wait_for_slot(item_id, token, holder):
            return "stale"

        claimed = job_store.transition_item(
            item_id,
            {DownloadStatus.QUEUED},
            lambda cur: {
                "status": DownloadStatus.DOWNLOADING,
                "attempts": int(cur.get("attempts") or 0) + 1,
                "started_at": cur.get("started_at") or time.time(),
                "finished_at": None,
                "error": None if int(cur.get("attempts") or 0) == 0 else cur.get("error"),
                "control": None,
                "stage": "starting",
            },
            expect_token=token,
        )
        if claimed is None:
            slot_limiter.release(holder)
            return "stale"

        ctx = _Ctx(claimed, job)
        hb = _Heartbeat(item_id, holder, self.heartbeat_interval)
        hb.start()
        try:
            outcome = self._execute(ctx)
        finally:
            hb.stop()
            slot_limiter.release(holder)
        if outcome in ("completed", "failed", "cancelled"):
            done = job_store.get_item(item_id)
            analytics.finished(
                outcome, title=(done.title if done else "") or item.url, fmt=job.options.format.value,
                size=(done.filesize or 0) if done else 0, error=(done.error or "") if done else "", cache_hit=ctx.cache_hit,
            )
        if outcome in ("completed", "failed", "cancelled") and delivery_service.enabled:
            try:  # server mode: the last item to settle prepares the file / ZIP for the browser
                delivery_service.maybe_build(job.job_id)
            except Exception:
                logger.exception(f"building the delivery of job {job.job_id} failed (the sweep retries)")
        return outcome

    # ── slot waiting ────────────────────────────────────────────────

    def _wait_for_slot(self, item_id: str, token: str, holder: str) -> bool:
        """Block until a parallel-download slot is free (or the task went stale)."""
        while True:
            state = job_store.get_control_state(item_id)
            if (
                state is None
                or state["token"] != token
                or state["status"] != DownloadStatus.QUEUED.value
            ):
                return False
            if slot_limiter.acquire(holder, runtime_service.get().max_parallel):
                return True
            time.sleep(self.slot_poll)

    # ── execution + settling ────────────────────────────────────────

    def _execute(self, ctx: _Ctx) -> str:
        try:
            self._download(ctx)
        except DownloadInterrupted as exc:
            return self._on_interrupted(ctx, exc.action)
        except StaleTask:
            return "stale"
        except Exception as exc:
            return self._on_error(ctx, exc)
        return self._on_success(ctx)

    def _download(self, ctx: _Ctx) -> None:
        settings = get_settings()
        dl = settings.download
        item, job = ctx.item, ctx.job

        if get_ffmpeg_path() is None:
            raise RuntimeError(
                "ffmpeg is not installed — set DL_FFMPEG_PATH or install ffmpeg "
                "(the bundled imageio-ffmpeg binary was not found)"
            )

        outdir = (dl.downloads_dir / job.folder).resolve()
        if dl.downloads_dir not in outdir.parents and outdir != dl.downloads_dir:
            raise RuntimeError("Refusing to write outside the downloads directory")
        # (outdir is NOT created here: yt-dlp creates it when it moves the finished file in, so a
        #  failed or cancelled download never leaves an empty folder behind)
        # Private scratch dir per item: .part files, intermediates, thumbnails, subtitles. Two jobs for the
        # same title (e.g. mp3 + opus, or mp3 + mp4) can no longer trample each other's files; only the
        # finished file is moved into `outdir`. Stable per item, so a resume finds its .part files again.
        ctx.tmp_dir = dl.downloads_dir / ".tmp" / item.item_id
        ctx.tmp_dir.mkdir(parents=True, exist_ok=True)

        prefix = ""
        if job.kind.is_multi and ctx.options.numbering:
            width = max(2, len(str(max(job.playlist_size, job.total_items, item.index))))
            prefix = f"{item.index:0{width}d} - "

        with cookie_service.scope(job.owner):  # server mode: the visitor's own cookies (or none)
            self._run_ydl(ctx, outdir, prefix)

        if not ctx.final:
            ctx.final = self._final_from_result(ctx.result)
        if not ctx.final or not Path(ctx.final).exists():
            raise RuntimeError("Download finished but the output file could not be located")
        if not ctx.cache_hit:
            cache_service.store_media(ctx.info.get("id") or item.video_id, ctx.job.options, Path(ctx.final))

    def _run_ydl(self, ctx: _Ctx, outdir: Path, prefix: str) -> None:
        """Build the options and run yt-dlp — everything that talks to YouTube, inside one session."""
        item, job = ctx.item, ctx.job
        opts = build_ydl_opts(
            ctx.options,
            outdir=outdir,
            temp_dir=ctx.tmp_dir,
            filename_prefix=prefix,
            runtime_speed_limit=runtime_service.get().speed_limit,
            progress_hook=lambda d: self._on_progress(ctx, d),
            postprocessor_hook=lambda d: self._on_postprocess(ctx, d),
            post_hook=lambda path: setattr(ctx, "final", path),
        )

        with self.ydl_factory(opts) as ydl:
            ctx.ydl = ydl
            self._check(ctx, force=True)
            info = ydl.extract_info(item.url, download=False, process=False)
            ctx.info = info or {}
            self._apply_metadata(ctx, ctx.info)
            self._check(ctx, force=True)
            if get_settings().task.log_progress != "off":
                title = ctx.info.get("title") or ctx.item.title or ctx.item.url
                logger.info(f"[{ctx.tag}] start  {title[:70]}  ({ctx.options.format.value} → {job.folder}/)")
            # everything that talks to YouTube stays inside the `with`: leaving it closes the session, and
            # using `ydl` afterwards silently opens a new one that is never closed (leaked sockets → EMFILE)
            if ctx.info.get("is_live") or ctx.info.get("live_status") == "is_live":
                raise RuntimeError("Live streams are not supported")
            existing = None if ctx.options.overwrite else self._existing_output(ydl, ctx, prefix)
            if existing is None and not ctx.options.overwrite:
                existing = self._from_media_cache(ydl, ctx, prefix)
            if existing is not None:
                if not ctx.cache_hit:
                    logger.info(f"already downloaded, skipping: {existing.name}")
                ctx.final = str(existing)
                result = None
                ctx.result = None
            else:
                result = ydl.process_ie_result(info, download=True)
            ctx.result = result

    def _predicted_output(self, ydl: Any, ctx: _Ctx, prefix: str) -> Path | None:
        """
        The path of the finished file this item will produce.

        yt-dlp only knows the *pre*-conversion name (e.g. ``.webm``), so we predict
        the final one ourselves (same template + target extension).
        """
        suffix = " [%(id)s]" if get_settings().download.include_id_in_name else ""
        try:
            stem = ydl.prepare_filename(ctx.info, outtmpl=f"{prefix}%(title).150B{suffix}")
        except Exception:
            return None
        return Path(f"{stem}.{ctx.job.options.format.value}")

    def _existing_output(self, ydl: Any, ctx: _Ctx, prefix: str) -> Path | None:
        """
        The finished file this item would produce, if it is already on disk.

        yt-dlp only looks for the *pre*-conversion name (e.g. ``.webm``), which
        is gone after an mp3 conversion — so it would re-download and re-encode.
        """
        candidate = self._predicted_output(ydl, ctx, prefix)
        try:
            return candidate if candidate and candidate.is_file() and candidate.stat().st_size > 0 else None
        except OSError:
            return None

    def _from_media_cache(self, ydl: Any, ctx: _Ctx, prefix: str) -> Path | None:
        """
        Server mode: another job produced exactly this file recently — link it into
        this job's folder instead of downloading and converting it again.
        """
        src = cache_service.lookup_media(ctx.info.get("id") or ctx.item.video_id, ctx.job.options)
        target = self._predicted_output(ydl, ctx, prefix) if src else None
        if src is None or target is None:
            return None
        try:
            if target.resolve() != src:
                cache_service.place(src, target)
        except OSError as exc:
            logger.info(f"[{ctx.tag}] media cache unusable ({exc}) — downloading")
            return None
        ctx.cache_hit = True
        logger.info(f"[{ctx.tag}] served from the media cache: {target.name}")
        return target

    @staticmethod
    def _final_from_result(result: Any) -> str | None:
        if not isinstance(result, dict):
            return None
        for rd in result.get("requested_downloads") or []:
            if rd.get("filepath"):
                return rd["filepath"]
        return result.get("filepath")

    def _on_success(self, ctx: _Ctx) -> str:
        item_id = ctx.item.item_id
        final = Path(ctx.final)  # type: ignore[arg-type]
        control = (job_store.get_control_state(item_id) or {}).get("control")
        if control == ControlAction.CANCEL.value:
            # cancel arrived during post-processing — honour it, drop the output
            final.unlink(missing_ok=True)
            self._cleanup(ctx)
            job_store.transition_item(
                item_id, _RUNNING, self._settle_patch(DownloadStatus.CANCELLED), expect_token=ctx.token
            )
            return "cancelled"

        downloads_dir = get_settings().download.downloads_dir
        try:
            rel = final.resolve().relative_to(downloads_dir)
            filepath = rel.as_posix()
        except ValueError:
            filepath = final.name
        done = job_store.transition_item(
            item_id,
            _RUNNING,
            {
                **self._settle_patch(DownloadStatus.COMPLETED),
                "percent": 100.0,
                "filepath": filepath,
                "filesize": final.stat().st_size,
                "downloaded_bytes": final.stat().st_size,
                "total_bytes": final.stat().st_size,
                "error": None,
            },
            expect_token=ctx.token,
        )
        self._drop_tmp(ctx)
        if done:
            took = human_duration(time.monotonic() - ctx.started)
            logger.success(f"completed [{ctx.tag}] {ctx.item.title or item_id} → {filepath}  ({human_bytes(final.stat().st_size)} in {took})")
        return "completed"

    def _on_interrupted(self, ctx: _Ctx, action: str) -> str:
        item_id = ctx.item.item_id
        if action == ControlAction.PAUSE.value:
            job_store.transition_item(
                item_id,
                _RUNNING,
                {**self._settle_patch(DownloadStatus.PAUSED), "partials": self._partials(ctx)},
                expect_token=ctx.token,
            )
            logger.info(f"paused {item_id}")
            return "paused"

        # cancel (explicit, or the item/job was deleted under us)
        self._cleanup(ctx)
        if job_store.get_control_state(item_id) is None:
            logger.info(f"removed {item_id} (record deleted) — partial files cleaned")
            return "removed"
        job_store.transition_item(
            item_id, _RUNNING, self._settle_patch(DownloadStatus.CANCELLED), expect_token=ctx.token
        )
        logger.info(f"cancelled {item_id}")
        return "cancelled"

    def _on_error(self, ctx: _Ctx, exc: BaseException) -> str:
        item_id = ctx.item.item_id
        message, retryable = friendly_error(exc)
        s = get_settings().task
        max_attempts = 1 + s.retry_count
        attempts = ctx.item.attempts

        if retryable and attempts < max_attempts:
            new_token = new_id()
            moved = job_store.transition_item(
                item_id,
                _RUNNING,
                {
                    **self._settle_patch(DownloadStatus.QUEUED, keep_token=False),
                    "token": new_token,
                    "error": f"Retrying ({attempts}/{max_attempts - 1}): {message}",
                    "finished_at": None,
                },
                expect_token=ctx.token,
            )
            if moved:
                logger.warning(f"retrying {item_id} in {s.retry_delay}s: {message}")
                get_dispatcher().enqueue_item(item_id, new_token, delay=s.retry_delay)
                return "retry"
            return "stale"

        if isinstance(exc, (RuntimeError, ValueError)) or not retryable:
            logger.error(f"failed {item_id}: {message}")
        else:
            logger.error(f"failed {item_id}: {message}", exc_info=exc)
        job_store.transition_item(
            item_id,
            _RUNNING,
            {
                **self._settle_patch(DownloadStatus.FAILED),
                "error": message,
                "partials": self._partials(ctx),
            },
            expect_token=ctx.token,
        )
        return "failed"

    @staticmethod
    def _settle_patch(status: DownloadStatus, keep_token: bool = False) -> dict[str, Any]:
        """Common fields when an item leaves the running states."""
        patch: dict[str, Any] = {
            "status": status,
            "control": None,
            "speed": None,
            "eta": None,
            "stage": "",
            "finished_at": time.time(),
        }
        if not keep_token:
            patch["token"] = ""  # invalidates any other queued task for this item
        if status in (DownloadStatus.CANCELLED,):
            patch.update(percent=0.0, downloaded_bytes=0, total_bytes=None)
        if status in (DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED):
            patch["force"] = False  # a "restart/replace" request is spent once the run ends
        return patch

    # ── hooks ───────────────────────────────────────────────────────

    def _check(self, ctx: _Ctx, force: bool = False) -> None:
        """Honour pause/cancel requests (throttled; ``force`` skips the throttle)."""
        now = time.monotonic()
        if not force and now - ctx.last_check < _CHECK_INTERVAL:
            return
        ctx.last_check = now
        state = job_store.get_control_state(ctx.item.item_id)
        if state is None:
            raise DownloadInterrupted(ControlAction.CANCEL.value)  # record deleted
        if state["token"] != ctx.token:
            raise StaleTask()
        if state["control"] in (ControlAction.PAUSE.value, ControlAction.CANCEL.value):
            raise DownloadInterrupted(state["control"])

    def _on_progress(self, ctx: _Ctx, d: dict[str, Any]) -> None:
        self._check(ctx)
        status = d.get("status")
        for key in ("filename", "tmpfilename"):
            if d.get(key):
                ctx.seen.add(d[key])

        if status == "finished":
            ctx.streams_done += 1
            ctx.bytes_prev += int(d.get("total_bytes") or d.get("downloaded_bytes") or 0)
            return
        if status != "downloading":
            return

        info_d = d.get("info_dict") or {}
        if ctx.n_streams is None:
            # first stream is video-only (no audio) ⇒ yt-dlp will fetch audio next
            video_only = info_d.get("acodec") in (None, "none") and info_d.get("vcodec") not in (None, "none")
            ctx.n_streams = 2 if video_only else 1

        now = time.monotonic()
        if now - ctx.last_write < _CHECK_INTERVAL:
            return
        ctx.last_write = now

        done = int(d.get("downloaded_bytes") or 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if total:
            pct = done / total * 100
        elif d.get("fragment_count"):
            pct = (d.get("fragment_index") or 0) / d["fragment_count"] * 100
        else:
            pct = 0.0
        pct = max(0.0, min(pct, 100.0))

        if ctx.n_streams == 2:
            overall = pct * _VIDEO_WEIGHT if ctx.streams_done == 0 else 100 * _VIDEO_WEIGHT + pct * (1 - _VIDEO_WEIGHT)
            stage = "video" if ctx.streams_done == 0 else "audio"
        else:
            overall, stage = pct, "downloading"

        job_store.patch_item(
            ctx.item.item_id,
            percent=round(min(overall, 99.5), 1),
            downloaded_bytes=ctx.bytes_prev + done,
            total_bytes=int(total) if (total and ctx.n_streams == 1) else None,
            speed=d.get("speed"),
            eta=d.get("eta"),
            stage=stage,
        )
        shown = round(min(overall, 99.5), 1)
        if progress_log_due(get_settings().task.log_progress, shown, ctx.last_log_pct, now, ctx.last_log_t):
            ctx.last_log_pct, ctx.last_log_t = shown, now
            title = ctx.info.get("title") or ctx.item.title or ""
            logger.info(progress_line(ctx.tag, title, shown, d.get("speed"), d.get("eta"), stage))

    def _on_postprocess(self, ctx: _Ctx, d: dict[str, Any]) -> None:
        if d.get("status") != "started":
            return
        label = _PP_LABELS.get(d.get("postprocessor", ""), d.get("postprocessor", "processing"))
        if get_settings().task.log_progress != "off":
            logger.info(f"[{ctx.tag}] {label}…")
        job_store.transition_item(
            ctx.item.item_id,
            _RUNNING,
            {"status": DownloadStatus.PROCESSING, "stage": label, "speed": None, "eta": None, "percent": 99.5},
            expect_token=ctx.token,
        )

    # ── metadata + cleanup ──────────────────────────────────────────

    def _apply_metadata(self, ctx: _Ctx, info: dict[str, Any]) -> None:
        """Fill title/thumbnail/duration as soon as extraction yields them."""
        title = (info.get("title") or "").strip()
        patch: dict[str, Any] = {}
        if title and not ctx.item.title.strip():
            patch["title"] = title
        elif title and ctx.job.kind is JobKind.SINGLE:
            patch["title"] = title
        for field, key in (("uploader", "uploader"), ("duration", "duration"), ("video_id", "id")):
            if info.get(key) is not None:
                patch[field] = info[key]
        if info.get("channel") and "uploader" not in patch:
            patch["uploader"] = info["channel"]
        if info.get("thumbnail"):
            patch["thumbnail"] = info["thumbnail"]
        if patch:
            job_store.patch_item(ctx.item.item_id, **patch)
        if ctx.job.kind is JobKind.SINGLE and title:
            job_store.update_job(
                ctx.job.job_id,
                title=title,
                uploader=patch.get("uploader"),
                thumbnail=patch.get("thumbnail"),
            )

    def _cleanup(self, ctx: _Ctx) -> None:
        """Delete everything an item that will not finish left behind (its private temp dir)."""
        leftovers = set(ctx.item.partials)
        if ctx.tmp_dir is not None:
            leftovers.add(str(ctx.tmp_dir))
        remove_partial_files(leftovers)

    @staticmethod
    def _partials(ctx: _Ctx) -> list[str]:
        """What to delete if the user later cancels / restarts this item: its temp directory."""
        return [str(ctx.tmp_dir)] if ctx.tmp_dir is not None else []

    @staticmethod
    def _drop_tmp(ctx: _Ctx) -> None:
        """The item finished: its scratch directory is no longer needed."""
        if ctx.tmp_dir is not None:
            shutil.rmtree(ctx.tmp_dir, ignore_errors=True)


download_runner = DownloadRunner()
