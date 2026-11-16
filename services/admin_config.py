"""
AdminConfigService — the admin panel: change selected settings live, without a restart.

A fixed whitelist of "knobs" (``KNOBS``) maps to fields of the cached ``Settings`` (``section.field``).
Overrides are stored in Redis and applied to the settings object of EVERY process (API and worker):

    P:CONFIG        HASH    "section.field" → JSON value (only the overridden ones)
    P:CONFIG:VER    STRING  bumped on every change; each process polls it every few seconds

Values from ``.env`` stay the defaults: removing an override restores them. Paths, Redis, the admin
password, ``SERVER_MODE`` and anything else that needs a restart is deliberately NOT a knob.

Hard ceilings live here too: ``task.max_parallel_cap`` (≤ the worker threads) and
``server.max_speed`` — nobody can set a parallel limit or a speed above them (see ``runtime`` / ``manager``).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from configs import get_key_prefix, get_redis, get_settings
from models import AUDIO_BITRATES, AUDIO_FORMATS, VIDEO_FORMATS, VIDEO_HEIGHTS
from modules import get_logger, parse_size
from services.errors import InvalidRequestError

logger = get_logger(__name__)

_POLL_S = 3.0


@dataclass(frozen=True)
class Knob:
    """One setting the admin may change at runtime."""

    key: str  # "section.field" of configs.Settings
    group: str
    label: str
    help: str
    kind: str  # int | bool | choice | speed | text
    min: int | None = None
    max: int | None = None
    choices: tuple[str, ...] = field(default_factory=tuple)
    server_only: bool = False  # only meaningful in server mode (still editable)


KNOBS: tuple[Knob, ...] = (
    # maintenance
    Knob("server.maintenance", "Maintenance", "Maintenance mode",
         "Refuse new downloads (running ones finish). Visitors see the message below.", "bool", server_only=True),
    Knob("server.maintenance_message", "Maintenance", "Message to visitors", "", "text", server_only=True),
    # hard ceilings
    Knob("task.max_parallel_cap", "Hard limits", "Max parallel downloads",
         "Ceiling for the parallel-downloads setting — nobody can go above it. At most the number of worker threads.",
         "int", 1, None),
    Knob("server.max_speed", "Hard limits", "Max speed per download",
         "Ceiling for every speed cap (bytes/sec, e.g. 2M). Downloads without a cap are held to it too. Empty = none.",
         "speed", 0, None, server_only=True),
    Knob("server.max_active_jobs", "Hard limits", "Unfinished downloads per visitor", "", "int", 1, 1000, server_only=True),
    Knob("server.max_job_items", "Hard limits", "Tracks per playlist / cart", "", "int", 1, 5000, server_only=True),
    Knob("server.max_subscriptions", "Hard limits", "Subscriptions per visitor", "Followed channels / playlists.", "int", 1, 1000, server_only=True),
    Knob("download.max_playlist_items", "Hard limits", "Playlist entries read", "Only the first N entries of a playlist are listed.", "int", 1, 5000),
    Knob("server.rate_limit_per_minute", "Hard limits", "Requests per IP per minute", "Preview / search / submit.", "int", 1, 10000, server_only=True),
    Knob("server.min_free_mb", "Hard limits", "Minimum free disk (MB)", "New downloads are refused below this.", "int", 0, 10_000_000, server_only=True),
    # retention
    Knob("server.delivery_ttl_minutes", "Retention", "Keep finished files (minutes)", "How long a prepared file / ZIP stays downloadable.", "int", 1, 10080, server_only=True),
    Knob("server.visitor_cookie_hours", "Retention", "Keep visitor cookies (hours)", "", "int", 1, 720, server_only=True),
    Knob("server.cache_minutes", "Retention", "Preview / search cache (minutes)", "0 = no caching.", "int", 0, 1440, server_only=True),
    Knob("server.admin_session_hours", "Retention", "Admin login lasts (hours)", "", "int", 1, 720),
    Knob("server.analytics_days", "Retention", "Keep statistics (days)", "", "int", 1, 365),
    Knob("task.record_retention_days", "Retention", "Keep job records (days)", "", "int", 1, 365),
    # access
    Knob("server.connect_enabled", "Access", "Sign in with the browser extension",
         "Lets people link their YouTube account with one click through the extension instead of pasting cookies. Pasting stays available.", "bool"),
    Knob("server.share_server_cookies", "Access", "Visitors may use the server's cookies",
         "Off: visitors use their own cookies or none.", "bool", server_only=True),
    # download defaults
    Knob("download.default_format", "Download defaults", "Default format", "",
         "choice", choices=tuple(sorted(f.value for f in AUDIO_FORMATS | VIDEO_FORMATS))),
    Knob("download.default_audio_quality", "Download defaults", "Default audio bitrate (kbps)", "", "choice", choices=(*AUDIO_BITRATES, "best")),
    Knob("download.default_video_quality", "Download defaults", "Default video resolution", "", "choice", choices=(*VIDEO_HEIGHTS, "best")),
    Knob("download.embed_metadata", "Download defaults", "Embed tags", "", "bool"),
    Knob("download.embed_thumbnail", "Download defaults", "Embed cover art", "", "bool"),
    Knob("download.playlist_numbering", "Download defaults", "Number playlist tracks", "", "bool"),
    Knob("download.include_id_in_name", "Download defaults", "Add the video id to file names", "", "bool"),
    # engine
    Knob("download.concurrent_fragments", "Engine", "Parallel fragments per download", "", "int", 1, 32),
    Knob("download.network_retries", "Engine", "Network retries (yt-dlp)", "", "int", 0, 50),
    Knob("task.retry_count", "Engine", "Automatic retries", "For transient failures.", "int", 0, 10),
    Knob("task.retry_delay", "Engine", "Retry delay (seconds)", "", "int", 0, 3600),
    Knob("download.lyrics_url", "Engine", "Lyrics provider URL", "Empty = lyrics off.", "text"),
)
_BY_KEY = {k.key: k for k in KNOBS}


class AdminConfigService:
    """Singleton: ``admin_config``."""

    def __init__(self) -> None:
        self._base: dict[str, Any] | None = None  # the .env / default value of every knob
        self._version: str | None = None
        self._lock = threading.Lock()
        self._watcher: threading.Thread | None = None

    # ── keys / helpers ──────────────────────────────────────────────

    @property
    def _hkey(self) -> str:
        return f"{get_key_prefix()}:CONFIG"

    @property
    def _vkey(self) -> str:
        return f"{get_key_prefix()}:CONFIG:VER"

    @staticmethod
    def _target(key: str) -> tuple[Any, str]:
        section, name = key.split(".", 1)
        return getattr(get_settings(), section), name

    def _current(self, key: str) -> Any:
        obj, name = self._target(key)
        return getattr(obj, name)

    def _capture_base(self) -> dict[str, Any]:
        if self._base is None:
            self._base = {k.key: self._current(k.key) for k in KNOBS}
        return self._base

    def coerce(self, key: str, value: Any) -> Any:
        """
        Validate one value for a knob.

        Raises:
            InvalidRequestError: unknown key or a bad value (the message names the setting).
        """
        knob = _BY_KEY.get(key)
        if knob is None:
            raise InvalidRequestError(f"Unknown setting: {key}")
        try:
            if knob.kind == "bool":
                if isinstance(value, str):
                    value = value.strip().lower() in ("1", "true", "yes", "on")
                return bool(value)
            if knob.kind == "speed":
                if value in (None, "", 0, "0"):
                    return None
                v = parse_size(value)
                if v < 1024:
                    raise ValueError("at least 1K")
                return v
            if knob.kind == "int":
                v = int(value)
                hi = knob.max
                if key == "task.max_parallel_cap":
                    hi = get_settings().task.workers  # threads are the physical ceiling
                if (knob.min is not None and v < knob.min) or (hi is not None and v > hi):
                    raise ValueError(f"between {knob.min} and {hi}")
                return v
            if knob.kind == "choice":
                v = str(value).strip().lower()
                if v not in knob.choices:
                    raise ValueError("one of " + ", ".join(knob.choices))
                return v
            v = str(value or "").strip()
            return v or None
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError(f"{knob.label}: must be {exc}") from exc

    # ── read / write ────────────────────────────────────────────────

    def list(self) -> list[dict[str, Any]]:
        """Every knob with its value, its .env default and whether it is overridden."""
        base = self._capture_base()
        overridden = set(get_redis().hkeys(self._hkey))
        out = []
        workers = get_settings().task.workers
        for k in KNOBS:
            hi = workers if k.key == "task.max_parallel_cap" else k.max
            out.append({
                "key": k.key, "group": k.group, "label": k.label, "help": k.help, "kind": k.kind,
                "min": k.min, "max": hi, "choices": list(k.choices), "server_only": k.server_only,
                "value": min(self._current(k.key), workers) if k.key == "task.max_parallel_cap" else self._current(k.key),
                "default": base[k.key], "overridden": k.key in overridden,
            })
        return out

    def update(self, values: dict[str, Any], reset: list[str] | tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """
        Set several knobs at once (everything is validated before anything is written).

        Args:
            values: ``{"section.field": value}`` — for a speed or text knob ``None`` / ``""`` means "none".
            reset: knobs to put back to their ``.env`` value.

        Raises:
            InvalidRequestError: a bad key or value — nothing is changed then.
        """
        self._capture_base()
        sets = {key: self.coerce(key, value) for key, value in values.items()}
        for key in reset:
            if key not in _BY_KEY:
                raise InvalidRequestError(f"Unknown setting: {key}")
        r = get_redis()
        pipe = r.pipeline()
        if sets:
            pipe.hset(self._hkey, mapping={k: json.dumps(v) for k, v in sets.items()})
        for key in reset:
            pipe.hdel(self._hkey, key)
        pipe.incr(self._vkey)
        pipe.execute()
        logger.info(f"admin config changed: {sorted([*sets, *reset])}")
        self.apply(force=True)
        return self.list()

    def reset(self, key: str) -> list[dict[str, Any]]:
        """Remove one override."""
        if key not in _BY_KEY:
            raise InvalidRequestError(f"Unknown setting: {key}")
        pipe = get_redis().pipeline()
        pipe.hdel(self._hkey, key)
        pipe.incr(self._vkey)
        pipe.execute()
        self.apply(force=True)
        return self.list()

    # ── apply in this process ───────────────────────────────────────

    def apply(self, force: bool = False) -> bool:
        """Load the overrides into this process's settings (only when they changed). Returns True if applied."""
        r = get_redis()
        version = r.get(self._vkey) or "0"
        if not force and version == self._version:
            return False
        with self._lock:
            base = self._capture_base()
            stored = r.hgetall(self._hkey)
            for k in KNOBS:
                value = base[k.key]
                if k.key in stored:
                    try:
                        value = self.coerce(k.key, json.loads(stored[k.key]))
                    except (InvalidRequestError, ValueError):
                        logger.warning(f"ignoring invalid stored value for {k.key}")
                obj, name = self._target(k.key)
                setattr(obj, name, value)
            self._version = version
        return True

    def start_watcher(self) -> None:
        """Poll for changes made by another process (API ↔ worker). Started once per process."""
        if self._watcher is not None:
            return

        def loop() -> None:
            stop = threading.Event()
            while not stop.wait(_POLL_S):
                try:
                    self.apply()
                except Exception:  # Redis hiccup: try again next round
                    logger.debug("admin config poll failed", exc_info=True)

        try:
            self.apply(force=True)
        except Exception:
            logger.warning("admin config could not be loaded at start-up", exc_info=True)
        self._watcher = threading.Thread(target=loop, name="admin-config", daemon=True)
        self._watcher.start()


admin_config = AdminConfigService()
