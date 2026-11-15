"""
Application settings — single source of truth for all configuration.

Resolution order (highest priority wins):
    1. Real environment variables (Docker -e, shell export)
    2. .env file (absolute path — independent of process CWD)
    3. Field defaults

Keys are matched case-insensitively against ``env_prefix + field name``:
    REDIS_URL / REDIS_KEY_PREFIX
    API_HOST / API_PORT / API_DEBUG
    DL_DOWNLOADS_DIR / DL_TIMEZONE / DL_SPEED_LIMIT ...
    TASK_WORKERS / TASK_MAX_PARALLEL / TASK_RETRY_COUNT ...
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from modules.generics import parse_size

# Repo root = parent of configs/ — makes .env lookup CWD-independent
# (works from anywhere: uv scripts, Docker WORKDIR, huey workers, tests)
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

_COMMON = {"env_file": ENV_FILE, "extra": "ignore"}


class RedisSettings(BaseSettings):
    """Broker + state store. Env: REDIS_URL, REDIS_KEY_PREFIX."""

    redis_url: str = "redis://localhost:6379/0"
    # Namespaces every key we write so a shared Redis stays tidy.
    redis_key_prefix: str = "YTDL"

    model_config = SettingsConfigDict(**_COMMON)


class FastAPISettings(BaseSettings):
    """HTTP server knobs. Env: API_*."""

    debug: bool = False
    # Loopback by default: the app fetches URLs on the caller's behalf.
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = ["*"]

    model_config = SettingsConfigDict(env_prefix="api_", **_COMMON)


class DownloadSettings(BaseSettings):
    """Download behaviour and yt-dlp tuning. Env: DL_*."""

    downloads_dir: Path = Path("downloads")
    timezone: str = "Asia/Kolkata"
    date_folder_format: str = "%Y-%m-%d"
    playlist_numbering: bool = True
    include_id_in_name: bool = False
    max_playlist_items: int = 500

    default_format: str = "mp3"
    default_audio_quality: str = "192"
    default_video_quality: str = "1080"

    concurrent_fragments: int = 4
    network_retries: int = 5
    speed_limit: int | None = None  # bytes/sec; accepts "2M", "500K"
    embed_metadata: bool = True
    embed_thumbnail: bool = True

    ffmpeg_path: str | None = None
    js_runtime: str = "auto"  # auto | deno | node | bun | quickjs
    cookies_file: Path | None = None
    cookies_from_browser: str | None = None
    default_folder: str | None = None  # base folder inside downloads/ (runtime-adjustable in the UI)
    proxy: str | None = None
    # Lyrics provider for the player (LRCLIB API); empty = lyrics off. Only artist / title / duration are sent.
    lyrics_url: str | None = "https://lrclib.net"
    allowed_hosts: list[str] = [
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    ]

    model_config = SettingsConfigDict(env_prefix="dl_", **_COMMON)

    @property
    def cookies_path(self) -> Path:
        """Cookies file in use: ``DL_COOKIES_FILE`` or ``<repo>/cookies/cookies.txt`` (the web UI writes here)."""
        p = self.cookies_file or Path("cookies") / "cookies.txt"
        return p if p.is_absolute() else BASE_DIR / p

    @field_validator("downloads_dir", mode="after")
    @classmethod
    def _absolute_downloads_dir(cls, v: Path) -> Path:
        """Relative download dirs resolve against the repo root, not the CWD."""
        return (v if v.is_absolute() else BASE_DIR / v).resolve()

    @field_validator("speed_limit", mode="before")
    @classmethod
    def _parse_speed_limit(cls, v):
        """Accept '', None, ints and human sizes like '2M'."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return parse_size(v)

    @field_validator(
        "ffmpeg_path", "cookies_file", "cookies_from_browser", "proxy", "default_folder", "lyrics_url", mode="before"
    )
    @classmethod
    def _blank_is_none(cls, v):
        """`KEY=` in .env arrives as '' — treat it as unset."""
        return None if isinstance(v, str) and not v.strip() else v


class TaskSettings(BaseSettings):
    """Huey queue + scheduling limits. Env: TASK_*."""

    workers: int = 6  # consumer threads: hard ceiling for parallel downloads
    max_parallel: int = 3  # soft limit; adjustable at runtime
    max_parallel_cap: int = 12
    retry_count: int = 1
    retry_delay: int = 10
    record_retention_days: int = 30
    immediate: bool = False  # inline execution + in-memory storage (tests)
    log_progress: Literal["off", "milestones", "verbose"] = "milestones"  # progress lines in the worker log

    model_config = SettingsConfigDict(env_prefix="task_", **_COMMON)


class ServerSettings(BaseSettings):
    """
    Public / multi-user deployment. Env: SERVER_*.

    ``SERVER_MODE=false`` (default) keeps the personal, single-user behaviour.
    ``true``: files are prepared per job in ``downloads/.deliveries/<job>/``, handed to the
    browser (one file, or a ZIP), and deleted ``SERVER_DELIVERY_TTL_MINUTES`` later; every
    visitor only sees their own queue; library edits, cookies and runtime settings need the
    admin password.
    """

    mode: bool = False
    # the /admin dashboard login: username + "scrypt:…" hash from `uv run python main.py hash-password`
    admin_username: str = "admin"
    admin_password_hash: str | None = None
    admin_session_hours: int = Field(default=12, ge=1)
    delivery_ttl_minutes: int = Field(default=60, ge=1)
    max_active_jobs: int = Field(default=5, ge=1)  # unfinished downloads per visitor
    max_job_items: int = Field(default=100, ge=1)  # tracks per playlist / cart
    min_free_mb: int = Field(default=2048, ge=0)  # refuse new downloads below this much free disk
    rate_limit_per_minute: int = Field(default=60, ge=1)  # info / search / submit calls per IP
    cache_minutes: int = Field(default=10, ge=0)  # preview / search / playlist cache (0 = off)
    visitor_cookie_hours: int = Field(default=24, ge=1)  # how long a visitor's own pasted cookies are kept
    share_server_cookies: bool = False  # let visitors use the server's (admin's) cookies — off: their own or none
    max_speed: int | None = None  # hard ceiling per download (bytes/sec; accepts "2M"); nobody can set more
    maintenance: bool = False  # server mode: refuse new downloads (the message below is shown to visitors)
    maintenance_message: str = "Downloads are paused for maintenance — please try again later."
    analytics_days: int = Field(default=30, ge=1)  # how long daily statistics are kept

    @field_validator("max_speed", mode="before")
    @classmethod
    def _parse_max_speed(cls, v):
        """Accept '', None, ints and human sizes like '2M'."""
        if v is None or (isinstance(v, str) and not v.strip()) or v == 0:
            return None
        return parse_size(v)

    model_config = SettingsConfigDict(env_prefix="server_", **_COMMON)

    @field_validator("admin_password_hash", mode="before")
    @classmethod
    def _blank_hash_is_none(cls, v):
        """`SERVER_ADMIN_PASSWORD_HASH=` → no admin (management stays locked)."""
        return None if isinstance(v, str) and not v.strip() else v


class Settings(BaseSettings):
    """Aggregated settings — access as settings.download.downloads_dir, etc."""

    redis: RedisSettings = RedisSettings()
    api: FastAPISettings = FastAPISettings()
    download: DownloadSettings = DownloadSettings()
    task: TaskSettings = TaskSettings()
    server: ServerSettings = ServerSettings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings. Never construct ``Settings()`` ad-hoc."""
    return Settings()
