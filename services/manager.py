"""
DownloadManager — the orchestration API of the backend.

Everything the HTTP layer (or a CLI / IPython session) does goes through here:

    submit()                     validate URLs → create jobs/items → enqueue
    resolve_job()                (task) expand a playlist into items
    pause/resume/cancel/retry    per item, per job, or for everything
    delete_job / clear_finished  remove records (optionally the files too)
    snapshot / list / get        read models for the UI
    reconcile()                  heal state after a crashed worker

State lives in Redis (``services.store``); work is handed to the queue only via
``services.dispatch`` — services never import ``tasks``.
"""

from __future__ import annotations

import time

from pydantic import ValidationError

from configs import get_settings
from models import (
    ControlAction,
    DownloadItem,
    DownloadJob,
    DownloadOptions,
    DownloadRequest,
    DownloadStatus,
    JobKind,
    JobView,
    MediaFormat,
    QueueStats,
    ResolveState,
)
from modules import get_logger, new_id
from modules.ytdlp_util import parse_selection, remove_partial_files
from services.dispatch import get_dispatcher
from services.errors import ConflictError, InvalidRequestError, NotFoundError
from services.metadata import metadata_service
from services.runtime import runtime_service, slot_limiter
from services.store import job_store
from services.ytdl_opts import friendly_error

logger = get_logger(__name__)

_RUNNING = (DownloadStatus.DOWNLOADING, DownloadStatus.PROCESSING)
_STALE_AFTER_S = 60  # running item with no live slot + no update this long = dead
_RESOLVE_TIMEOUT_S = 300


def _thumb(video_id: str | None) -> str | None:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg" if video_id else None


class DownloadManager:
    """Singleton: ``download_manager``."""

    def __init__(self, store=job_store):
        self.store = store

    # ═══════════════════════════ submit ═══════════════════════════

    def resolve_options(self, req: DownloadRequest) -> DownloadOptions:
        """Apply settings defaults to a request and validate the combination."""
        dl = get_settings().download
        try:
            fmt = req.format or MediaFormat(dl.default_format)
            quality = req.quality or (
                dl.default_audio_quality if fmt.is_audio else dl.default_video_quality
            )
            return DownloadOptions(
                format=fmt,
                quality=quality,
                embed_metadata=dl.embed_metadata if req.embed_metadata is None else req.embed_metadata,
                embed_thumbnail=dl.embed_thumbnail if req.embed_thumbnail is None else req.embed_thumbnail,
                subtitles=req.subtitles,
                subtitle_langs=req.subtitle_langs,
                speed_limit=req.speed_limit,
                overwrite=req.overwrite,
                numbering=dl.playlist_numbering if req.numbering is None else req.numbering,
            )
        except (ValidationError, ValueError) as exc:
            detail = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            raise InvalidRequestError(detail.removeprefix("Value error, ")) from exc

    def submit(self, req: DownloadRequest) -> tuple[list[JobView], list[dict[str, str]]]:
        """
        Create one job per URL and enqueue them.

        Bad URLs do not poison the batch: they are reported in ``rejected``
        while the valid ones proceed.

        Returns:
            ``(jobs, rejected)`` where ``rejected`` is ``[{"url", "error"}]``.

        Raises:
            InvalidRequestError: the options are invalid, or EVERY url was rejected.
        """
        options = self.resolve_options(req)
        jobs: list[DownloadJob] = []
        rejected: list[dict[str, str]] = []

        for url in req.urls:
            try:
                target = metadata_service.resolve_target(url, req.playlist_mode)
                if target.kind is JobKind.PLAYLIST and req.playlist_items:
                    parse_selection(req.playlist_items, 10**6)  # syntax check only
                jobs.append(self._create(target, req, options))
            except InvalidRequestError as exc:
                rejected.append({"url": url, "error": str(exc)})
            except ValueError as exc:
                rejected.append({"url": url, "error": f"Bad playlist selection: {exc}"})

        if not jobs:
            raise InvalidRequestError(rejected[0]["error"] if len(rejected) == 1 else "; ".join(r["error"] for r in rejected))
        return [self.get_job_view(j.job_id, include_items=True) for j in jobs], rejected

    def _create(self, target, req: DownloadRequest, options: DownloadOptions) -> DownloadJob:
        job_id = new_id()
        if target.kind is JobKind.SINGLE:
            item_id, token = new_id(), new_id()
            job = DownloadJob(
                job_id=job_id,
                url=target.url,
                kind=JobKind.SINGLE,
                options=options,
                folder=metadata_service.folder_for_single(req.folder),
                resolve_state=ResolveState.READY,
                total_items=1,
                thumbnail=_thumb(target.parsed.video_id),
            )
            item = DownloadItem(
                item_id=item_id,
                job_id=job_id,
                url=target.url,
                video_id=target.parsed.video_id,
                thumbnail=_thumb(target.parsed.video_id),
                token=token,
            )
            self.store.create_job(job)
            self.store.add_items([item])
            get_dispatcher().enqueue_item(item_id, token)
            logger.info(f"queued single {target.url} → job {job_id}")
            return job

        job = DownloadJob(
            job_id=job_id,
            url=target.url,
            kind=JobKind.PLAYLIST,
            options=options,
            folder_override=req.folder,
            playlist_items=req.playlist_items,
            resolve_state=ResolveState.PENDING,
        )
        self.store.create_job(job)
        get_dispatcher().enqueue_resolve(job_id)
        logger.info(f"queued playlist {target.url} → job {job_id}")
        return job

    # ═══════════════════════════ resolve (task body) ═══════════════════════════

    def resolve_job(self, job_id: str) -> None:
        """Expand a PENDING playlist job into items and enqueue them."""
        if self.store.transition_job(job_id, {ResolveState.PENDING.value}, {"resolve_state": ResolveState.RESOLVING}) is None:
            return  # gone, cancelled, or another task already took it
        job = self.store.get_job(job_id)
        if job is None:
            return

        def fail(message: str) -> None:
            self.store.transition_job(
                job_id,
                {ResolveState.RESOLVING.value},
                {"resolve_state": ResolveState.FAILED, "resolve_error": message},
            )
            logger.error(f"resolve failed {job_id}: {message}")

        try:
            playlist = metadata_service.expand_playlist(job.url)
        except Exception as exc:
            fail(friendly_error(exc)[0])
            return
        total = len(playlist.entries)
        if total == 0:
            fail("The playlist is empty or not accessible")
            return
        try:
            wanted = set(parse_selection(job.playlist_items, total))
        except ValueError as exc:
            fail(str(exc))
            return

        items: list[DownloadItem] = []
        for e in playlist.entries:
            if e.index not in wanted:
                continue
            ok = e.available and e.url
            items.append(
                DownloadItem(
                    item_id=new_id(),
                    job_id=job_id,
                    index=e.index,
                    url=e.url,
                    video_id=e.video_id,
                    title=e.title,
                    thumbnail=e.thumbnail,
                    duration=e.duration,
                    status=DownloadStatus.QUEUED if ok else DownloadStatus.FAILED,
                    token=new_id() if ok else "",
                    error=None if ok else "Unavailable (private, deleted or region-locked)",
                    finished_at=None if ok else time.time(),
                )
            )

        ready = self.store.transition_job(
            job_id,
            {ResolveState.RESOLVING.value},
            {
                "title": playlist.title,
                "uploader": playlist.uploader,
                "thumbnail": playlist.thumbnail,
                "folder": metadata_service.folder_for_playlist(playlist.title, job.folder_override),
                "playlist_size": total,
                "total_items": len(items),
                "resolve_state": ResolveState.READY,
                "resolve_error": None,
            },
        )
        if ready is None:
            return  # cancelled while resolving
        self.store.add_items(items)
        dispatcher = get_dispatcher()
        for it in items:
            if it.status is DownloadStatus.QUEUED:
                dispatcher.enqueue_item(it.item_id, it.token)
        logger.info(f"resolved {job_id}: '{playlist.title}' → {len(items)}/{total} items")

    # ═══════════════════════════ item controls ═══════════════════════════

    def _require_item(self, item_id: str) -> DownloadItem:
        item = self.store.get_item(item_id)
        if item is None:
            raise NotFoundError(f"Item {item_id} not found")
        return item

    def pause_item(self, item_id: str) -> DownloadItem:
        """
        Pause an item.

        Queued → paused immediately (its pending task becomes stale).
        Running → a pause request the worker honours within ~0.5 s, keeping the
        partial file so resume continues where it stopped.
        """
        self._require_item(item_id)
        moved = self.store.transition_item(
            item_id,
            {DownloadStatus.QUEUED},
            {"status": DownloadStatus.PAUSED, "token": "", "control": None, "stage": "", "speed": None, "eta": None},
        )
        if moved:
            return moved
        requested = self.store.transition_item(
            item_id, _RUNNING, {"control": ControlAction.PAUSE}
        )
        if requested:
            return requested
        raise ConflictError(f"Cannot pause an item that is {self._require_item(item_id).status.value}")

    def cancel_item(self, item_id: str) -> DownloadItem:
        """Cancel an item and remove its partial files."""
        item = self._require_item(item_id)
        moved = self.store.transition_item(
            item_id,
            {DownloadStatus.QUEUED, DownloadStatus.PAUSED},
            {
                "status": DownloadStatus.CANCELLED,
                "token": "",
                "control": None,
                "stage": "",
                "speed": None,
                "eta": None,
                "percent": 0.0,
                "downloaded_bytes": 0,
                "total_bytes": None,
                "finished_at": time.time(),
                "partials": [],
            },
        )
        if moved:
            remove_partial_files(item.partials)
            return moved
        requested = self.store.transition_item(item_id, _RUNNING, {"control": ControlAction.CANCEL})
        if requested:
            return requested
        raise ConflictError(f"Cannot cancel an item that is {self._require_item(item_id).status.value}")

    def resume_item(self, item_id: str) -> DownloadItem:
        """Re-queue a PAUSED item; yt-dlp continues from the ``.part`` file."""
        return self._requeue(item_id, {DownloadStatus.PAUSED}, reset_attempts=False)

    def retry_item(self, item_id: str) -> DownloadItem:
        """Re-queue a FAILED or CANCELLED item with a fresh attempt budget."""
        return self._requeue(
            item_id, {DownloadStatus.FAILED, DownloadStatus.CANCELLED}, reset_attempts=True
        )

    def restart_item(self, item_id: str) -> DownloadItem:
        """
        Start an item over from scratch (also *replaces* a finished one).

        Discards partial files and ignores any existing output, so the new run
        re-downloads and overwrites it. A completed item's old file stays until
        the new one succeeds — a failed replacement never destroys the original.
        """
        item = self._require_item(item_id)
        token = new_id()
        moved = self.store.transition_item(
            item_id,
            {DownloadStatus.PAUSED, DownloadStatus.FAILED, DownloadStatus.CANCELLED, DownloadStatus.COMPLETED},
            {
                "status": DownloadStatus.QUEUED,
                "token": token,
                "control": None,
                "error": None,
                "stage": "",
                "finished_at": None,
                "attempts": 0,
                "force": True,
                "percent": 0.0,
                "downloaded_bytes": 0,
                "total_bytes": None,
                "speed": None,
                "eta": None,
                "partials": [],
            },
        )
        if moved is None:
            raise ConflictError(f"Cannot restart an item that is {item.status.value}")
        remove_partial_files(item.partials)
        get_dispatcher().enqueue_item(item_id, token)
        return moved

    def _requeue(self, item_id: str, allowed: set[DownloadStatus], *, reset_attempts: bool) -> DownloadItem:
        item = self._require_item(item_id)
        token = new_id()
        patch = {
            "status": DownloadStatus.QUEUED,
            "token": token,
            "control": None,
            "error": None,
            "stage": "",
            "finished_at": None,
        }
        if reset_attempts:
            patch["attempts"] = 0
        moved = self.store.transition_item(item_id, allowed, patch)
        if moved is None:
            raise ConflictError(f"Cannot {'resume' if DownloadStatus.PAUSED in allowed else 'retry'} an item that is {item.status.value}")
        get_dispatcher().enqueue_item(item_id, token)
        return moved

    # ═══════════════════════════ job + bulk controls ═══════════════════════════

    def _require_job(self, job_id: str) -> DownloadJob:
        job = self.store.get_job(job_id)
        if job is None:
            raise NotFoundError(f"Job {job_id} not found")
        return job

    def _apply(self, job_id: str, fn) -> int:
        """Apply an item action to every item of a job, skipping conflicts."""
        changed = 0
        for item in self.store.get_items(job_id):
            try:
                fn(item.item_id)
                changed += 1
            except ConflictError:
                continue
        return changed

    def pause_job(self, job_id: str) -> JobView:
        self._require_job(job_id)
        self._apply(job_id, self.pause_item)
        return self.get_job_view(job_id, include_items=True)

    def resume_job(self, job_id: str) -> JobView:
        self._require_job(job_id)
        self._apply(job_id, self.resume_item)
        return self.get_job_view(job_id, include_items=True)

    def _cancel(self, job_id: str) -> int:
        """Abort a still-resolving playlist and cancel unfinished items; returns how many changed."""
        aborted = self.store.transition_job(
            job_id,
            {ResolveState.PENDING.value, ResolveState.RESOLVING.value},
            {"resolve_state": ResolveState.FAILED, "resolve_error": "Cancelled"},
        )
        return self._apply(job_id, self.cancel_item) + (1 if aborted else 0)

    def _retry(self, job_id: str, item_fn) -> int:
        """Re-queue failed items — or re-run a playlist expansion that failed before any item existed."""
        job = self._require_job(job_id)
        if job.resolve_state is ResolveState.FAILED and not self.store.get_item_ids(job_id):
            self.store.update_job(job_id, resolve_state=ResolveState.PENDING, resolve_error=None)
            get_dispatcher().enqueue_resolve(job_id)
            return 1
        return self._apply(job_id, item_fn)

    def cancel_job(self, job_id: str) -> JobView:
        """Cancel every unfinished item; a still-resolving playlist is aborted."""
        self._require_job(job_id)
        self._cancel(job_id)
        return self.get_job_view(job_id, include_items=True)

    def retry_job(self, job_id: str) -> JobView:
        """Retry failed/cancelled items (or re-run a failed playlist expansion)."""
        self._retry(job_id, self.retry_item)
        return self.get_job_view(job_id, include_items=True)

    def restart_job(self, job_id: str) -> JobView:
        """Start every restartable item over from scratch (replaces finished files)."""
        self._retry(job_id, self.restart_item)
        return self.get_job_view(job_id, include_items=True)

    def bulk_jobs(self, job_ids: list[str], action: str) -> dict:
        """
        Apply one action to many jobs.

        Args:
            job_ids: jobs to act on (duplicates ignored).
            action: ``pause | resume | cancel | retry | restart | remove | remove-files``.

        Returns:
            ``{"affected": jobs that changed, "skipped": jobs where nothing applied,
            "errors": [{"id", "error"}]}`` — a missing id never aborts the batch.

        Raises:
            InvalidRequestError: unknown action.
        """
        item_actions = {
            "pause": lambda j: self._apply(j, self.pause_item),
            "resume": lambda j: self._apply(j, self.resume_item),
            "cancel": self._cancel,
            "retry": lambda j: self._retry(j, self.retry_item),
            "restart": lambda j: self._retry(j, self.restart_item),
        }
        if action not in item_actions and action not in ("remove", "remove-files"):
            raise InvalidRequestError(f"Unknown bulk action: {action}")
        affected = skipped = 0
        errors: list[dict[str, str]] = []
        for jid in dict.fromkeys(job_ids):
            try:
                self._require_job(jid)
                if action in ("remove", "remove-files"):
                    self.delete_job(jid, delete_files=action == "remove-files")
                    changed = 1
                else:
                    changed = item_actions[action](jid)
            except NotFoundError as exc:
                errors.append({"id": jid, "error": str(exc)})
                continue
            if changed:
                affected += 1
            else:
                skipped += 1
        return {"affected": affected, "skipped": skipped, "errors": errors}

    def _all_job_ids(self) -> list[str]:
        return self.store.list_job_ids(0, 1000)

    def pause_all(self) -> int:
        return sum(self._apply(j, self.pause_item) for j in self._all_job_ids())

    def resume_all(self) -> int:
        return sum(self._apply(j, self.resume_item) for j in self._all_job_ids())

    def cancel_all(self) -> int:
        n = 0
        for jid in self._all_job_ids():
            self.store.transition_job(
                jid,
                {ResolveState.PENDING.value, ResolveState.RESOLVING.value},
                {"resolve_state": ResolveState.FAILED, "resolve_error": "Cancelled"},
            )
            n += self._apply(jid, self.cancel_item)
        return n

    def retry_failed(self) -> int:
        """Retry FAILED items only (cancelled ones were the user's choice)."""
        n = 0
        for jid in self._all_job_ids():
            for item in self.store.get_items(jid):
                if item.status is DownloadStatus.FAILED:
                    try:
                        self.retry_item(item.item_id)
                        n += 1
                    except ConflictError:
                        pass
        return n

    # ═══════════════════════════ removal ═══════════════════════════

    def delete_job(self, job_id: str, *, delete_files: bool = False) -> dict[str, int]:
        """
        Remove a job's records (running items are aborted and their partial files
        cleaned by the worker). With ``delete_files`` also removes the finished
        files and the job folder when it ends up empty.
        """
        job = self._require_job(job_id)
        items = self.store.get_items(job_id)
        removed_files = 0
        if delete_files:
            root = get_settings().download.downloads_dir
            for it in items:
                if it.filepath:
                    f = (root / it.filepath).resolve()
                    if root in f.parents and f.is_file():
                        f.unlink(missing_ok=True)
                        removed_files += 1
                remove_partial_files(it.partials)
            folder = (root / job.folder).resolve() if job.folder else None
            if folder and root in folder.parents and folder.is_dir():
                try:
                    folder.rmdir()  # only succeeds when empty
                except OSError:
                    pass
        n = self.store.delete_job(job_id)
        return {"items": n, "files": removed_files}

    def clear_finished(self, *, delete_files: bool = False) -> int:
        """Delete every job whose status is terminal (completed/failed/cancelled/partial)."""
        cleared = 0
        for view in self.list_job_views(limit=1000):
            if view.status.is_terminal:
                self.delete_job(view.job_id, delete_files=delete_files)
                cleared += 1
        return cleared

    # ═══════════════════════════ reads ═══════════════════════════

    def get_item(self, item_id: str) -> DownloadItem:
        return self._require_item(item_id)

    def get_job_view(self, job_id: str, *, include_items: bool = True) -> JobView:
        job = self._require_job(job_id)
        return JobView.build(job, self.store.get_items(job_id), include_items=include_items)

    def _views(
        self, limit: int, offset: int, items_for: str = "none"
    ) -> list[JobView]:
        """
        Build views for the newest jobs.

        ``items_for``: ``"none"`` | ``"active"`` | ``"all"``. ``"active"`` embeds items
        for unfinished jobs AND for single-video jobs (one tiny item, and the UI
        needs its file path / error even after it finished); finished playlists
        are fetched on demand via ``GET /api/downloads/{id}``.
        """
        ids = self.store.list_job_ids(offset, limit)
        jobs = self.store.get_jobs(ids)
        items = self.store.get_items_many([j.job_id for j in jobs])
        views = []
        for j in jobs:
            view = JobView.build(j, items[j.job_id], include_items=False)
            if items_for == "all" or (
                items_for == "active"
                and (not view.status.is_terminal or j.kind is JobKind.SINGLE)
            ):
                view.items = items[j.job_id]
            views.append(view)
        return views

    def list_job_views(
        self,
        *,
        status: DownloadStatus | None = None,
        limit: int = 50,
        offset: int = 0,
        include_items: bool = False,
    ) -> list[JobView]:
        """Newest-first jobs; ``status`` filters on the derived job status."""
        if status is None:
            return self._views(limit, offset, "all" if include_items else "none")
        views = [v for v in self._views(1000, 0, "all" if include_items else "none") if v.status is status]
        return views[offset : offset + limit]

    def snapshot(self, limit: int = 200) -> tuple[list[JobView], QueueStats]:
        """Jobs (items embedded for unfinished ones) + stats — one SSE frame."""
        views = self._views(limit, 0, "active")
        return views, self.stats(views)

    def stats(self, views: list[JobView] | None = None) -> QueueStats:
        views = views if views is not None else self._views(1000, 0, "none")
        c = lambda *names: sum(v.counts.get(n, 0) for v in views for n in names)  # noqa: E731
        return QueueStats(
            total_jobs=self.store.count_jobs(),
            total_items=sum(v.total_items for v in views),
            active=c("downloading", "processing"),
            queued=c("queued"),
            paused=c("paused"),
            completed=c("completed"),
            failed=c("failed"),
            cancelled=c("cancelled"),
            speed=sum(v.speed for v in views),
            downloaded_bytes=sum(v.downloaded_bytes for v in views),
            max_parallel=runtime_service.get().max_parallel,
            workers=get_settings().task.workers,
        )

    # ═══════════════════════════ maintenance ═══════════════════════════

    def reconcile(self) -> int:
        """
        Heal records a crashed worker left behind.

        * running items with no live slot and no update for a minute → PAUSED
          ("interrupted") so the user can resume them
        * playlist jobs stuck expanding for minutes → FAILED

        Returns:
            Number of records repaired.
        """
        now = time.time()
        fixed = 0
        live = slot_limiter.live_items()
        for jid in self._all_job_ids():
            job = self.store.get_job(jid)
            if job is None:
                continue
            if (
                job.resolve_state in (ResolveState.PENDING, ResolveState.RESOLVING)
                and now - job.updated_at > _RESOLVE_TIMEOUT_S
            ):
                self.store.transition_job(
                    jid,
                    {ResolveState.PENDING.value, ResolveState.RESOLVING.value},
                    {"resolve_state": ResolveState.FAILED, "resolve_error": "Timed out while reading the playlist"},
                )
                fixed += 1
            for it in self.store.get_items(jid):
                if (
                    it.status in _RUNNING
                    and now - it.updated_at > _STALE_AFTER_S
                    and it.item_id not in live
                ):
                    moved = self.store.transition_item(
                        it.item_id,
                        _RUNNING,
                        {
                            "status": DownloadStatus.PAUSED,
                            "token": "",
                            "control": None,
                            "speed": None,
                            "eta": None,
                            "stage": "",
                            "error": "Interrupted (worker stopped) — resume to continue",
                        },
                    )
                    fixed += 1 if moved else 0
        if fixed:
            logger.warning(f"reconcile repaired {fixed} record(s)")
        return fixed


download_manager = DownloadManager()
