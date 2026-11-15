"""
CacheService — server-mode caches that save YouTube round trips and whole downloads.

    JSON cache   previews, searches and playlist expansions (``SERVER_CACHE_MINUTES``). Many visitors
                 look at the same links; the resolve task re-reads the playlist the user just previewed.
    media cache  "video X as mp3 192k with these embed options" → a finished file of some job. Another
                 visitor asking for the same thing within the delivery TTL gets a hard link (or a copy)
                 instead of a fresh download + ffmpeg run. Entries expire with the delivery TTL and are
                 verified on every hit (the file may already have been swept).

Personal mode never reads or writes these (``enabled`` is False).

Keys:
    P:META:<sha1>    STRING  JSON value
    P:MEDIA:<sha1>   STRING  path of a finished file, relative to the downloads dir
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from configs import get_key_prefix, get_redis, get_settings
from models import DownloadOptions
from modules import get_logger

logger = get_logger(__name__)


def _digest(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()


class CacheService:
    """Singleton: ``cache_service``."""

    @property
    def enabled(self) -> bool:
        return get_settings().server.mode

    # ── JSON (metadata) ─────────────────────────────────────────────

    def get_json(self, kind: str, *key: Any) -> Any | None:
        """A cached value, or None (also when caching is off or Redis hiccups)."""
        s = get_settings().server
        if not self.enabled or s.cache_minutes <= 0:
            return None
        try:
            raw = get_redis().get(f"{get_key_prefix()}:META:{_digest(kind, *key)}")
            return json.loads(raw) if raw else None
        except Exception:  # a cache must never break the request
            logger.debug("meta cache read failed", exc_info=True)
            return None

    def set_json(self, kind: str, value: Any, *key: Any) -> None:
        s = get_settings().server
        if not self.enabled or s.cache_minutes <= 0:
            return
        try:
            get_redis().set(
                f"{get_key_prefix()}:META:{_digest(kind, *key)}", json.dumps(value), ex=s.cache_minutes * 60
            )
        except Exception:
            logger.debug("meta cache write failed", exc_info=True)

    # ── media files ─────────────────────────────────────────────────

    @staticmethod
    def media_key(video_id: str, options: DownloadOptions) -> str:
        """Everything that changes the produced file (format, quality, tags, cover, subtitles, naming)."""
        o = options
        return _digest(
            "media", video_id, o.format.value, o.quality, o.embed_metadata, o.embed_thumbnail,
            o.subtitles, ",".join(o.subtitle_langs), get_settings().download.include_id_in_name,
        )

    def lookup_media(self, video_id: str | None, options: DownloadOptions) -> Path | None:
        """A finished file for this video + options that still exists, or None."""
        if not self.enabled or not video_id:
            return None
        try:
            rel = get_redis().get(f"{get_key_prefix()}:MEDIA:{self.media_key(video_id, options)}")
        except Exception:
            return None
        if not rel:
            return None
        root = get_settings().download.downloads_dir
        p = (root / rel).resolve()
        try:
            if root in p.parents and p.is_file() and p.stat().st_size > 0:
                return p
        except OSError:
            pass
        return None

    def store_media(self, video_id: str | None, options: DownloadOptions, path: Path) -> None:
        """Remember a finished file for the next visitor (TTL = the delivery TTL)."""
        if not self.enabled or not video_id:
            return
        root = get_settings().download.downloads_dir
        try:
            rel = path.resolve().relative_to(root).as_posix()
            ttl = get_settings().server.delivery_ttl_minutes * 60
            get_redis().set(f"{get_key_prefix()}:MEDIA:{self.media_key(video_id, options)}", rel, ex=ttl)
        except (ValueError, OSError):
            pass
        except Exception:
            logger.debug("media cache write failed", exc_info=True)

    @staticmethod
    def place(src: Path, dst: Path) -> None:
        """
        Put a cached file at ``dst``: a hard link (instant, no extra disk) or a copy.
        Each job owns its own link, so sweeping one job never breaks another's file.

        Raises:
            OSError: neither worked (the caller falls back to a real download).
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)


cache_service = CacheService()
