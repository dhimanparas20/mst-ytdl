"""
Client singletons — one instance per process, built from configs.config.

Usage:
    from configs.clients import get_redis, get_ffmpeg_path, get_js_runtimes

Rules:
    - Always call the get_*() functions. Never construct clients ad-hoc.
    - get_*() are @lru_cached → same object every call, created lazily on first
      use (so a missing Redis doesn't crash ``import``).
    - Huey tasks are sync by design → use this sync client there; FastAPI
      endpoints that touch Redis are plain ``def`` (threadpool) or offload.
    - Tests swap the Redis client with ``set_redis_override(fakeredis...)``.
"""

from __future__ import annotations

from functools import lru_cache

import redis

from configs.config import get_settings
from modules.ytdlp_util import find_ffmpeg, find_js_runtime

_redis_override: redis.Redis | None = None


def get_redis_url() -> str:
    """Redis connection URL from settings (cheap — no connection made)."""
    return get_settings().redis.redis_url


def get_key_prefix() -> str:
    """Namespace prefix for every Redis key (``REDIS_KEY_PREFIX``)."""
    return get_settings().redis.redis_key_prefix


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    """Shared SYNC Redis client (``decode_responses=True``)."""
    if _redis_override is not None:
        return _redis_override
    return redis.Redis.from_url(
        get_redis_url(), decode_responses=True, health_check_interval=30
    )


def set_redis_override(client: redis.Redis | None) -> None:
    """Replace the shared client (tests). Pass ``None`` to restore the real one."""
    global _redis_override
    _redis_override = client
    get_redis.cache_clear()


@lru_cache(maxsize=1)
def get_ffmpeg_path() -> str | None:
    """ffmpeg executable (configured → PATH → bundled), or None."""
    return find_ffmpeg(get_settings().download.ffmpeg_path)


@lru_cache(maxsize=1)
def get_js_runtimes() -> dict | None:
    """``js_runtimes`` mapping for yt-dlp, or None when no runtime is installed."""
    return find_js_runtime(get_settings().download.js_runtime)
