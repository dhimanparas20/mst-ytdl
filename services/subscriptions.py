"""
SubscriptionService — follow a channel / playlist and download what is uploaded from now on.

    create(req, owner)        validate → snapshot options → SEED the seen-set with the current head → store
    list_for / get            records (+ ``seen_count``), newest first
    pause / resume / delete   per subscription
    check(id, owner)          poll one subscription now (manual "check")
    poll_all()                (periodic task) poll every enabled subscription; never raises

Redis (``P`` = ``REDIS_KEY_PREFIX``):
    P:SUB:<id>             HASH  Subscription, one JSON value per field (no TTL: lives until deleted)
    P:SUBS                 ZSET  every subscription id, scored by created_at
    P:OWNER_SUBS:<owner>   ZSET  server mode: the same, per visitor
    P:SUB_SEEN:<id>        SET   video ids already known (downloaded, skipped or present at subscribe time)
    P:SUB_LOCK:<id>        STR   per-subscription poll lock (TTL) — a manual check never overlaps a periodic one
    P:SUBS_LOCK            STR   whole-run lock (TTL) — two periodic runs never overlap

Semantics (the product decisions):
    * Subscribing seeds ``SEEN`` with whatever is on the channel / playlist NOW and downloads nothing:
      only uploads that appear afterwards are queued (no dumping of a whole back catalogue).
    * A poll reads the newest 15 uploads of a channel, or the whole playlist (new videos are
      usually appended at the end), and queues every id not yet in ``SEEN`` — oldest first, at most
      25 per poll; the rest wait for the next one.
    * Each video is claimed with ``SADD`` BEFORE it is submitted (atomic dedupe); a failed submit
      releases the claim, so it is retried by the next poll.
    * Shorts are marked seen but not downloaded while ``skip_shorts`` is on; live / premiere entries
      are looked at again later.
    * Pausing only stops polling. Resuming does not re-seed: uploads made while paused are picked
      up if they are still inside the polled window.
    * Server mode scopes everything to the visitor (``owner``); other visitors' records are 404.
      ``owner=None`` (personal mode, the periodic task) is unscoped.
"""

from __future__ import annotations

import time
from typing import Any

from configs import get_key_prefix, get_redis, get_settings
from models import (
    DownloadRequest,
    EntryInfo,
    PlaylistMode,
    Subscription,
    SubscriptionKind,
    SubscriptionRequest,
)
from modules import get_logger, human_bytes, new_id
from modules.redis_util import decode_hash, encode_hash
from services.cookies import cookie_service
from services.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    RateLimitedError,
    ServiceError,
)
from services.manager import download_manager
from services.metadata import metadata_service
from services.ytdl_opts import friendly_error, speed_ceiling

logger = get_logger(__name__)

PERSONAL_MAX = 100  # personal mode: followed channels / playlists (server mode: SERVER_MAX_SUBSCRIPTIONS)
CHANNEL_SCAN = 15  # newest uploads read from a channel per poll
MAX_QUEUE_PER_POLL = 25  # videos queued per subscription per poll
_LOCK_TTL_S = 600
_RUN_LOCK_TTL_S = 3600


class SubscriptionService:
    """Singleton: ``subscription_service``."""

    # ── keys ────────────────────────────────────────────────────────

    @property
    def r(self):
        return get_redis()

    @property
    def prefix(self) -> str:
        return get_key_prefix()

    def key(self, sub_id: str) -> str:
        return f"{self.prefix}:SUB:{sub_id}"

    def seen_key(self, sub_id: str) -> str:
        return f"{self.prefix}:SUB_SEEN:{sub_id}"

    def lock_key(self, sub_id: str) -> str:
        return f"{self.prefix}:SUB_LOCK:{sub_id}"

    @property
    def index(self) -> str:
        return f"{self.prefix}:SUBS"

    def owner_index(self, owner: str) -> str:
        return f"{self.prefix}:OWNER_SUBS:{owner}"

    def _index(self, owner: str | None) -> str:
        return self.owner_index(owner) if owner else self.index

    @property
    def server_mode(self) -> bool:
        return get_settings().server.mode

    def limit(self) -> int:
        """Subscriptions one owner may hold."""
        return get_settings().server.max_subscriptions if self.server_mode else PERSONAL_MAX

    # ── records ─────────────────────────────────────────────────────

    def get(self, sub_id: str, owner: str | None = None) -> Subscription:
        """One subscription (+ ``seen_count``).

        Raises:
            NotFoundError: unknown id, or another visitor's subscription.
        """
        sub = self._load(sub_id)
        if sub is None or (owner and sub.owner != owner):
            raise NotFoundError(f"Subscription {sub_id} not found")
        sub.seen_count = int(self.r.scard(self.seen_key(sub_id)))
        return sub

    def _load(self, sub_id: str) -> Subscription | None:
        raw = self.r.hgetall(self.key(sub_id))
        return Subscription.model_validate(decode_hash(raw)) if raw else None

    def list_for(self, owner: str | None = None) -> list[Subscription]:
        """Newest-first subscriptions of ``owner`` (``None`` = all). Index entries of deleted records are pruned."""
        index = self._index(owner)
        ids = self.r.zrevrange(index, 0, -1)
        out: list[Subscription] = []
        dead: list[str] = []
        for sid in ids:
            sub = self._load(sid)
            if sub is None:
                dead.append(sid)
                continue
            sub.seen_count = int(self.r.scard(self.seen_key(sid)))
            out.append(sub)
        if dead:
            self.r.zrem(index, *dead)
        return out

    def _patch(self, sub_id: str, fields: dict[str, Any] | None = None, incr: dict[str, int] | None = None) -> bool:
        """Patch fields (and add to integer ones) of an EXISTING record; False when it was deleted meanwhile."""
        key = self.key(sub_id)
        fields = dict(fields or {})

        def txn(pipe) -> bool:
            if not pipe.exists(key):
                return False
            patch = dict(fields)
            for name, delta in (incr or {}).items():
                cur = decode_hash({"v": pipe.hget(key, name) or "0"}).get("v") or 0
                patch[name] = int(cur) + delta
            pipe.multi()
            pipe.hset(key, mapping=encode_hash(patch))
            return True

        return bool(self.r.transaction(txn, key, value_from_callable=True))

    # ── create / controls ───────────────────────────────────────────

    def create(self, req: SubscriptionRequest, owner: str | None = None) -> Subscription:
        """
        Follow a channel or playlist. Reads its current head and marks it seen (nothing is downloaded now).

        Raises:
            InvalidRequestError: not a channel / playlist URL, bad options, or yt-dlp could not read it.
            ConflictError: already subscribed.
            RateLimitedError: the subscription limit is reached.
        """
        target = metadata_service.resolve_target(req.url, PlaylistMode.AUTO)
        parsed = target.parsed
        if parsed.path_kind == "video" and not parsed.list_id:
            raise InvalidRequestError("Subscriptions need a channel or playlist URL, not a single video")
        if parsed.list_id and parsed.list_id.startswith(("RD", "UL")):
            raise InvalidRequestError("Mixes and radios change constantly — subscribe to a channel or a regular playlist")
        if parsed.path_kind == "video":  # watch?v=..&list=..: it is the list that is followed
            target = metadata_service.resolve_target(req.url, PlaylistMode.PLAYLIST)
        kind = SubscriptionKind.CHANNEL if parsed.path_kind == "channel" else SubscriptionKind.PLAYLIST

        existing = self.list_for(owner)
        if len(existing) >= self.limit():
            raise RateLimitedError(f"You can follow at most {self.limit()} channels / playlists")
        if any(s.url == target.url for s in existing):
            raise ConflictError("You already follow this channel / playlist")

        options = download_manager.resolve_options(req).model_copy(update={"overwrite": False})
        ceiling = speed_ceiling() if self.server_mode else None
        if ceiling and options.speed_limit and options.speed_limit > ceiling:
            raise InvalidRequestError(f"The speed limit cannot exceed {human_bytes(ceiling)}/s on this server")

        try:
            with cookie_service.scope(owner):
                info = metadata_service.latest(target.url, self._scan_limit(kind))
        except InvalidRequestError:
            raise
        except Exception as exc:
            raise InvalidRequestError(friendly_error(exc)[0]) from exc

        now = time.time()
        sub = Subscription(
            sub_id=new_id(),
            url=target.url,
            kind=kind,
            title=info.title or info.uploader or target.url,
            uploader=info.uploader,
            thumbnail=info.thumbnail,
            options=options,
            skip_shorts=req.skip_shorts,
            owner=owner,
            created_at=now,
            last_check=now,
        )
        seed = [e.video_id for e in info.entries if e.video_id]
        pipe = self.r.pipeline()
        pipe.hset(self.key(sub.sub_id), mapping=encode_hash(sub.model_dump(mode="json", exclude={"seen_count"})))
        pipe.zadd(self.index, {sub.sub_id: now})
        if owner:
            pipe.zadd(self.owner_index(owner), {sub.sub_id: now})
        if seed:
            pipe.sadd(self.seen_key(sub.sub_id), *seed)
        pipe.execute()
        sub.seen_count = len(set(seed))
        logger.info(f"subscribed {sub.sub_id[:6]} {kind.value} '{sub.title}' ({sub.seen_count} existing videos marked seen)")
        return sub

    def _set_enabled(self, sub_id: str, owner: str | None, enabled: bool) -> Subscription:
        self.get(sub_id, owner)
        self._patch(sub_id, {"enabled": enabled})
        return self.get(sub_id, owner)

    def pause(self, sub_id: str, owner: str | None = None) -> Subscription:
        """Stop polling (the seen-set is kept)."""
        return self._set_enabled(sub_id, owner, False)

    def resume(self, sub_id: str, owner: str | None = None) -> Subscription:
        """Start polling again (uploads made meanwhile are queued if still inside the polled window)."""
        return self._set_enabled(sub_id, owner, True)

    def delete(self, sub_id: str, owner: str | None = None) -> None:
        """Forget a subscription and its seen-set (files already downloaded stay)."""
        sub = self.get(sub_id, owner)
        pipe = self.r.pipeline()
        pipe.delete(self.key(sub_id), self.seen_key(sub_id), self.lock_key(sub_id))
        pipe.zrem(self.index, sub_id)
        if sub.owner:
            pipe.zrem(self.owner_index(sub.owner), sub_id)
        pipe.execute()
        logger.info(f"unsubscribed {sub_id[:6]} '{sub.title}'")

    # ── polling ─────────────────────────────────────────────────────

    def _scan_limit(self, kind: SubscriptionKind) -> int:
        """Channels: only the newest uploads. Playlists: all of it (new entries are usually appended)."""
        return CHANNEL_SCAN if kind is SubscriptionKind.CHANNEL else get_settings().download.max_playlist_items

    def check(self, sub_id: str, owner: str | None = None) -> tuple[Subscription, int, str | None]:
        """
        Poll one subscription now (works while paused).

        Returns:
            ``(subscription, queued, error)``.

        Raises:
            NotFoundError: unknown / not yours.
            ConflictError: it is being polled right now.
        """
        sub = self.get(sub_id, owner)
        outcome = self._poll(sub)
        if outcome is None:
            raise ConflictError("This subscription is being checked right now")
        new, error = outcome
        try:
            fresh = self.get(sub_id, owner)
        except NotFoundError:
            fresh = sub
        return fresh, new, error

    def poll_all(self) -> dict[str, int]:
        """
        Poll every enabled subscription. Never raises: one failing subscription is recorded on itself
        and the loop goes on. Overlapping runs are refused through a Redis lock.

        Returns:
            ``{"checked": n, "queued": n}`` (zeros when skipped).
        """
        result = {"checked": 0, "queued": 0}
        s = get_settings().server
        if s.mode and s.maintenance:
            return result
        run_lock = f"{self.prefix}:SUBS_LOCK"
        try:
            if not self.r.set(run_lock, "1", nx=True, ex=_RUN_LOCK_TTL_S):
                return result
            try:
                for sub in self.list_for(None):
                    if not sub.enabled:
                        continue
                    try:
                        outcome = self._poll(sub)
                    except Exception:
                        logger.exception(f"subscription poll crashed for {sub.sub_id[:6]}")
                        continue
                    if outcome is not None:
                        result["checked"] += 1
                        result["queued"] += outcome[0]
            finally:
                self.r.delete(run_lock)
        except Exception:
            logger.exception("subscription polling failed")
        if result["queued"]:
            logger.info(f"subscriptions: {result['queued']} new video(s) queued from {result['checked']} checked")
        return result

    def _poll(self, sub: Subscription) -> tuple[int, str | None] | None:
        """One poll under the per-subscription lock; ``None`` = somebody else is polling it."""
        lock = self.lock_key(sub.sub_id)
        if not self.r.set(lock, "1", nx=True, ex=_LOCK_TTL_S):
            return None
        try:
            return self._poll_locked(sub)
        finally:
            self.r.delete(lock)

    def _poll_locked(self, sub: Subscription) -> tuple[int, str | None]:
        try:
            with cookie_service.scope(sub.owner):
                info = metadata_service.latest(sub.url, self._scan_limit(sub.kind))
        except Exception as exc:  # yt-dlp / network: recorded, never raised
            message = friendly_error(exc)[0]
            logger.warning(f"subscription {sub.sub_id[:6]} '{sub.title}': {message}")
            self._patch(sub.sub_id, {"last_check": time.time(), "last_error": message, "last_new": 0})
            return 0, message

        seen_key = self.seen_key(sub.sub_id)
        known = set(self.r.smembers(seen_key))
        fresh: list[EntryInfo] = []
        skipped: list[str] = []
        for e in info.entries:
            vid = e.video_id
            if not vid or vid in known or not e.available or vid in info.not_ready_ids:
                continue
            if sub.skip_shorts and vid in info.short_ids:
                skipped.append(vid)  # known, never downloaded
                continue
            fresh.append(e)
        if skipped:
            self.r.sadd(seen_key, *skipped)

        # chronological order: a channel lists newest first (reverse it); a playlist is already oldest first
        ordered = list(reversed(fresh)) if sub.kind is SubscriptionKind.CHANNEL else fresh
        new, error = self._enqueue(sub, ordered[:MAX_QUEUE_PER_POLL], seen_key)

        fields: dict[str, Any] = {"last_check": time.time(), "last_error": error, "last_new": new}
        if info.title:
            fields.update(title=info.title, uploader=info.uploader)
        if not self._patch(sub.sub_id, fields, incr={"downloaded": new}):
            # deleted while polling: do not leave an orphan seen-set behind
            self.r.delete(seen_key, self.lock_key(sub.sub_id))
        return new, error

    def _enqueue(self, sub: Subscription, entries: list[EntryInfo], seen_key: str) -> tuple[int, str | None]:
        """Queue each video (claim first, release on failure). Stops at the first refusal; returns (queued, error)."""
        new = 0
        for e in entries:
            if not self.r.sadd(seen_key, e.video_id):  # somebody else claimed it
                continue
            try:
                download_manager.submit(self._request(sub, e), owner=sub.owner)
            except ServiceError as exc:  # limits (429 / 503), bad options (422)
                self.r.srem(seen_key, e.video_id)
                logger.info(f"subscription {sub.sub_id[:6]} deferred: {exc}")
                return new, f"Waiting: {exc}"
            except Exception as exc:
                self.r.srem(seen_key, e.video_id)
                logger.exception(f"subscription {sub.sub_id[:6]}: could not queue {e.video_id}")
                return new, f"Could not queue a video: {exc}"
            new += 1
        return new, None

    def _request(self, sub: Subscription, entry: EntryInfo) -> DownloadRequest:
        """The submit body for one new video, from the options snapshotted at subscribe time."""
        o = sub.options
        # personal mode: one folder per channel / playlist (inside the default base folder); server mode ignores it
        folder = None if self.server_mode else metadata_service.folder_for_playlist(sub.title or "Subscription")[:120]
        return DownloadRequest(
            urls=[entry.url],
            playlist_mode=PlaylistMode.SINGLE,
            format=o.format,
            quality=o.quality,
            embed_metadata=o.embed_metadata,
            embed_thumbnail=o.embed_thumbnail,
            subtitles=o.subtitles,
            subtitle_langs=o.subtitle_langs,
            speed_limit=o.speed_limit,
            overwrite=False,
            sponsorblock=o.sponsorblock,
            numbering=o.numbering,
            folder=folder,
        )


subscription_service = SubscriptionService()
