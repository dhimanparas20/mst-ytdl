"""
DeliveryService — server mode: hand finished downloads to the browser, then delete them.

Layout (only ``.deliveries/<job_id>/`` directories are ever deleted, and only through the index):

    downloads/.deliveries/<job_id>/Title.mp3               single video → served as is
    downloads/.deliveries/<job_id>/<Name>/01 - Song.mp3     playlist / cart files …
    downloads/.deliveries/<job_id>/<Name>.zip               … zipped into one archive (stored, no recompression)

Every job owns its own directory, so visitors downloading the same song at the same time never share
(or delete) each other's files. The leading dot hides it from the library, zips and usage totals.

Lifecycle:
    1. the job finishes (every item settled) → :meth:`maybe_build` picks the file / builds the ZIP and
       writes a :class:`models.Delivery` record with ``expires_at = now + SERVER_DELIVERY_TTL_MINUTES``
    2. the browser fetches it (``GET /api/downloads/{id}/file``, owner-checked, Range-capable)
    3. optionally the visitor taps "Keep longer": :meth:`extend` adds one more TTL, once (``extended`` flag)
    4. :meth:`sweep` (Huey, every minute) purges expired jobs: Redis records, the job directory and the
       items' temp dirs. Safety nets: finished jobs without a record get one; paused jobs idle past the
       TTL are dropped; ``.deliveries`` / ``.tmp`` directories nobody references are removed once older
       than the TTL.

Keys (``P`` = prefix):
    P:DELIVERY:<job_id>        HASH   Delivery (one JSON value per field; ``extended`` = Keep longer used)
    P:DELIVERIES               ZSET   job id → expires_at (the cleanup index)
    P:DELIVERY_LOCK:<job_id>   STRING one builder at a time
"""

from __future__ import annotations

import hashlib
import re
import shutil
import time
import zipfile
from pathlib import Path

import redis

from configs import get_key_prefix, get_redis, get_settings
from models import (
    Delivery,
    DeliveryEntry,
    DeliveryKind,
    DeliveryView,
    DownloadItem,
    DownloadJob,
    DownloadStatus,
    JobKind,
    derive_job_status,
)
from modules import get_logger
from modules.redis_util import decode_hash, encode_hash
from modules.ytdlp_util import sanitize_filename
from services.errors import ConflictError, NotFoundError
from services.store import job_store

logger = get_logger(__name__)

DELIVERIES_DIR = ".deliveries"
_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
_LOCK_TTL_S = 600


class DeliveryService:
    """Singleton: ``delivery_service``."""

    # ── paths ───────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return get_settings().server.mode

    @property
    def ttl(self) -> int:
        """Seconds a finished delivery stays on disk."""
        return get_settings().server.delivery_ttl_minutes * 60

    @property
    def root(self) -> Path:
        return get_settings().download.downloads_dir / DELIVERIES_DIR

    def job_dir(self, job_id: str) -> Path:
        """
        ``downloads/.deliveries/<job_id>`` — validated, so a bad id can never point elsewhere.

        Raises:
            ValueError: ``job_id`` is not one of our ids.
        """
        if not _ID_RE.match(job_id or ""):
            raise ValueError(f"not a job id: {job_id!r}")
        return self.root / job_id

    def folder_for(self, job_id: str, name: str | None = None) -> str:
        """Job folder relative to the downloads dir: the job dir itself, or ``<job dir>/<Name>`` for many files."""
        base = f"{DELIVERIES_DIR}/{self.job_dir(job_id).name}"
        return f"{base}/{sanitize_filename(name, fallback='download')}" if name else base

    @staticmethod
    def is_delivery_folder(folder: str) -> bool:
        return folder == DELIVERIES_DIR or folder.startswith(DELIVERIES_DIR + "/")

    # ── records ─────────────────────────────────────────────────────

    def _key(self, job_id: str) -> str:
        return f"{get_key_prefix()}:DELIVERY:{job_id}"

    @property
    def _index(self) -> str:
        return f"{get_key_prefix()}:DELIVERIES"

    def get(self, job_id: str) -> Delivery | None:
        raw = get_redis().hgetall(self._key(job_id))
        return Delivery.model_validate(decode_hash(raw)) if raw else None

    def get_many(self, job_ids: list[str]) -> dict[str, Delivery]:
        """Batch read (one round trip); jobs without a record are absent."""
        if not job_ids:
            return {}
        pipe = get_redis().pipeline(transaction=False)
        for jid in job_ids:
            pipe.hgetall(self._key(jid))
        return {
            jid: Delivery.model_validate(decode_hash(raw)) for jid, raw in zip(job_ids, pipe.execute()) if raw
        }

    @staticmethod
    def view(d: Delivery | None) -> DeliveryView | None:
        """What the UI may see (nothing for a NONE record)."""
        if d is None or d.kind is DeliveryKind.NONE:
            return None
        return DeliveryView(kind=d.kind, name=d.name, size=d.size, expires_at=d.expires_at, extended=d.extended)

    def list_for_owner(self, owner: str | None, now: float | None = None) -> list[DeliveryEntry]:
        """
        The files / ZIPs a visitor can still save ("My downloads"), soonest-to-expire first.

        Driven by the delivery index (``P:DELIVERIES``) and filtered by ``Delivery.owner`` (falling
        back to the job's owner for older records). Only ready deliveries count: no NONE records,
        nothing expired and nothing whose file is gone. ``owner=None`` lists every delivery
        (unscoped). Personal mode has none.
        """
        if not self.enabled:
            return []
        now = time.time() if now is None else now
        # Active (not-yet-expired) ids from the index — survives "clear finished" as long as the
        # delivery record is kept; also merge the visitor's job list for records missing from the index.
        jids = list(get_redis().zrangebyscore(self._index, now, "+inf"))
        seen = set(jids)
        for jid in job_store.list_job_ids(0, 1000, owner):
            if jid not in seen:
                jids.append(jid)
                seen.add(jid)
        records = self.get_many(jids)
        jobs = {j.job_id: j for j in job_store.get_jobs(list(records.keys()))}
        items = job_store.get_items_many([jid for jid in records if jid in jobs])
        out: list[DeliveryEntry] = []
        for jid, d in records.items():
            if d.kind is DeliveryKind.NONE or d.expires_at <= now or self.file_path(d) is None:
                continue
            job = jobs.get(jid)
            rec_owner = d.owner if d.owner is not None else (job.owner if job else None)
            if owner is not None and rec_owner != owner:
                continue
            if job is not None:
                status = derive_job_status(job.resolve_state, job.resolve_error, items.get(jid) or [])
                title = job.title or d.name
            else:
                status = DownloadStatus.COMPLETED
                title = d.name
            out.append(
                DeliveryEntry(
                    job_id=jid, title=title, kind=d.kind, name=d.name, size=d.size,
                    expires_at=d.expires_at, extended=d.extended, status=status,
                )
            )
        return sorted(out, key=lambda e: e.expires_at)

    def extend(self, job_id: str, now: float | None = None) -> Delivery:
        """
        "Keep longer": push the expiry back by one TTL — once per delivery.

        An optimistic WATCH/MULTI transaction, so two taps (or two tabs) can never extend twice.

        Raises:
            NotFoundError: no ready delivery (never built, nothing finished, expired, or the file is gone).
            ConflictError: it was already extended.
        """
        now = time.time() if now is None else now
        r = get_redis()
        key = self._key(job_id)
        with r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    d = self.get(job_id)
                    if d is None or d.kind is DeliveryKind.NONE or d.expires_at <= now or self.file_path(d) is None:
                        raise NotFoundError("This download is no longer on the server")
                    if d.extended:
                        raise ConflictError("This download was already extended once")
                    d = d.model_copy(update={"expires_at": d.expires_at + self.ttl, "extended": True})
                    pipe.multi()
                    pipe.hset(key, mapping=encode_hash({"expires_at": d.expires_at, "extended": True}))
                    pipe.expire(key, int(d.expires_at - now) + 3600)
                    pipe.zadd(self._index, {job_id: d.expires_at})
                    pipe.execute()
                    break
                except redis.WatchError:
                    continue  # changed under us (sweep / a second tap): re-read and decide again
                finally:
                    pipe.reset()
        logger.info(f"delivery extended {job_id}: until {d.expires_at:.0f}")
        return d

    def _save(self, d: Delivery) -> None:
        r = get_redis()
        pipe = r.pipeline()
        pipe.delete(self._key(d.job_id))
        pipe.hset(self._key(d.job_id), mapping=encode_hash(d.model_dump(mode="json")))
        pipe.expire(self._key(d.job_id), self.ttl + 3600)  # the sweep normally removes it first
        pipe.zadd(self._index, {d.job_id: d.expires_at})
        pipe.execute()

    def file_path(self, d: Delivery) -> Path | None:
        """Absolute path of a ready delivery, if it is still on disk."""
        if d.kind is DeliveryKind.NONE or not d.path:
            return None
        root = get_settings().download.downloads_dir
        p = (root / d.path).resolve()
        return p if (self.root.resolve() in p.parents and p.is_file()) else None

    # ── build ───────────────────────────────────────────────────────

    @staticmethod
    def _finished(items: list[DownloadItem]) -> list[tuple[DownloadItem, Path]]:
        root = get_settings().download.downloads_dir
        out = []
        for it in sorted(items, key=lambda i: i.index):
            if it.status is DownloadStatus.COMPLETED and it.filepath:
                p = (root / it.filepath).resolve()
                if p.is_file():
                    out.append((it, p))
        return out

    @staticmethod
    def _signature(done: list[tuple[DownloadItem, Path]]) -> str:
        return hashlib.sha1(
            "|".join(f"{it.item_id}:{it.filepath}:{it.filesize}" for it, _ in done).encode()
        ).hexdigest()

    def maybe_build(self, job_id: str) -> Delivery | None:
        """
        Prepare what a FINISHED server-mode job hands to the browser (idempotent).

        A single video is served as is; several files are zipped (stored) into one archive.
        A job where nothing finished still gets a NONE record, so the sweep cleans it up.

        Returns:
            The delivery, or None when not applicable (personal mode, still running, already being built).
        """
        if not self.enabled:
            return None
        job = job_store.get_job(job_id)
        if job is None or not self.is_delivery_folder(job.folder):
            return None
        items = job_store.get_items(job_id)
        if not derive_job_status(job.resolve_state, job.resolve_error, items).is_terminal:
            return None
        done = self._finished(items)
        signature = self._signature(done)
        current = self.get(job_id)
        if current and current.signature == signature and (
            current.kind is DeliveryKind.NONE or self.file_path(current) is not None
        ):
            return current

        r = get_redis()
        lock = f"{get_key_prefix()}:DELIVERY_LOCK:{job_id}"
        if not r.set(lock, "1", nx=True, ex=_LOCK_TTL_S):
            return None  # another worker is on it
        try:
            d = self._build(job, done, signature)
            self._save(d)
            logger.info(f"delivery ready {job_id}: {d.kind.value} {d.name or '-'} ({d.size} bytes)")
            return d
        finally:
            r.delete(lock)

    def _build(self, job: DownloadJob, done: list[tuple[DownloadItem, Path]], signature: str) -> Delivery:
        now = time.time()
        root = get_settings().download.downloads_dir
        base = Delivery(job_id=job.job_id, owner=job.owner, signature=signature, ready_at=now, expires_at=now + self.ttl)
        if not done:
            return base  # kind NONE: only schedules the cleanup
        if job.kind is JobKind.SINGLE:
            _, p = done[0]
            return base.model_copy(
                update={"kind": DeliveryKind.FILE, "path": p.relative_to(root).as_posix(), "name": p.name, "size": p.stat().st_size}
            )
        folder = (root / job.folder).resolve()
        name = folder.name or "download"
        target = self.job_dir(job.job_id) / f"{name}.zip"
        part = target.with_name(target.name + ".part")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for _, p in done:
                zf.write(p, f"{name}/{p.name}")  # unzips into one folder named after the playlist / cart
        part.replace(target)
        return base.model_copy(
            update={"kind": DeliveryKind.ZIP, "path": target.relative_to(root).as_posix(), "name": target.name,
                    "size": target.stat().st_size}
        )

    # ── removal ─────────────────────────────────────────────────────

    def forget(self, job_id: str) -> None:
        """Drop the delivery record and delete the job's directory (files, ZIP, everything)."""
        r = get_redis()
        pipe = r.pipeline()
        pipe.delete(self._key(job_id))
        pipe.zrem(self._index, job_id)
        pipe.execute()
        try:
            shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
        except ValueError:
            pass  # not one of our ids: nothing on disk belongs to it

    def purge(self, job_id: str) -> None:
        """Remove a server-mode job completely: records (running workers notice and stop), files, temp dirs."""
        item_ids = job_store.get_item_ids(job_id)
        job_store.delete_job(job_id)
        self.forget(job_id)
        tmp = get_settings().download.downloads_dir / ".tmp"
        for iid in item_ids:
            if _ID_RE.match(iid):
                shutil.rmtree(tmp / iid, ignore_errors=True)

    # ── periodic cleanup ────────────────────────────────────────────

    def sweep(self, now: float | None = None) -> dict[str, int]:
        """
        Expire and heal (the Huey ``sweep_deliveries`` task, every minute). No-op in personal mode.

        Returns:
            Counts: ``expired`` jobs purged, ``built`` missing deliveries, ``idle`` paused jobs dropped,
            ``orphans`` unreferenced directories removed.
        """
        if not self.enabled:
            return {}
        now = time.time() if now is None else now
        counts = {"expired": 0, "built": 0, "idle": 0, "orphans": 0}
        r = get_redis()

        for jid in r.zrangebyscore(self._index, "-inf", now):
            self.purge(jid)
            counts["expired"] += 1

        for jid in job_store.list_job_ids(0, 100_000):
            job = job_store.get_job(jid)
            if job is None or not self.is_delivery_folder(job.folder):
                continue  # personal-mode jobs are never touched
            items = job_store.get_items(jid)
            status = derive_job_status(job.resolve_state, job.resolve_error, items)
            if status.is_terminal:
                if not r.exists(self._key(jid)) and self.maybe_build(jid):
                    counts["built"] += 1
            elif status is DownloadStatus.PAUSED:
                last = max([job.updated_at, *(i.updated_at for i in items)])
                if now - last > self.ttl:
                    self.purge(jid)
                    counts["idle"] += 1

        counts["orphans"] = self._sweep_orphans(now)
        if any(counts.values()):
            logger.info(f"delivery sweep: {counts}")
        return counts

    def _sweep_orphans(self, now: float) -> int:
        """Directories no record points at any more (a crash between steps), once older than the TTL."""
        removed = 0
        dl = get_settings().download.downloads_dir
        for parent, exists in ((self.root, job_store.get_job), (dl / ".tmp", job_store.get_item)):
            if not parent.is_dir():
                continue
            for d in parent.iterdir():
                if not d.is_dir() or not _ID_RE.match(d.name):
                    continue
                try:
                    old = now - d.stat().st_mtime > self.ttl
                except OSError:
                    continue
                if old and exists(d.name) is None:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
        return removed


delivery_service = DeliveryService()
