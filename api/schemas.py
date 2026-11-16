"""Request / response bodies that only the HTTP layer needs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from models import JobView, PlaylistMode


class RejectedUrl(BaseModel):
    url: str
    error: str


class SubmitResponse(BaseModel):
    """Result of ``POST /api/downloads``."""

    jobs: list[JobView]
    rejected: list[RejectedUrl] = Field(default_factory=list)


class InfoRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    playlist_mode: PlaylistMode = PlaylistMode.AUTO


class ActionResponse(BaseModel):
    """Outcome of a bulk action."""

    ok: bool = True
    affected: int = 0
    message: str = ""


class BulkJobsRequest(BaseModel):
    """``POST /api/downloads/bulk``."""

    ids: list[str] = Field(min_length=1, max_length=500)
    action: Literal["pause", "resume", "cancel", "retry", "restart", "remove", "remove-files"]


class BulkJobsResult(BaseModel):
    affected: int = 0  # jobs on which something changed
    skipped: int = 0  # jobs where the action did not apply (e.g. pause on a finished job)
    errors: list[dict[str, str]] = Field(default_factory=list)


class PathsRequest(BaseModel):
    """Library bulk body: relative paths under the downloads root."""

    paths: list[str] = Field(min_length=1, max_length=500)


class MoveRequest(PathsRequest):
    dest: str = Field(max_length=300)  # target folder (created if missing); '' = the downloads root


class RenameRequest(BaseModel):
    path: str
    name: str = Field(min_length=1, max_length=200)


class RuntimeUpdate(BaseModel):
    """``PUT /api/runtime`` — only fields that are present are changed."""

    max_parallel: int | None = Field(default=None, ge=1, le=64)
    # bytes/sec per download; send null or 0 to remove the cap
    speed_limit: int | None = Field(default=None, ge=0)
    # base folder inside downloads/ for new downloads; send null or "" to clear
    default_folder: str | None = Field(default=None, max_length=200)


class HealthResponse(BaseModel):
    status: str  # ok | degraded
    redis: bool
    worker: bool  # a Huey consumer heartbeat was seen recently
    ffmpeg: str | None = None
    js_runtime: str | None = None
    yt_dlp: str
    downloads_dir: str
    detail: list[str] = Field(default_factory=list)


class ConfigResponse(BaseModel):
    """Everything the UI needs to render its option pickers."""

    audio_formats: list[str]
    video_formats: list[str]
    audio_bitrates: list[str]
    video_heights: list[str]
    default_format: str
    default_audio_quality: str
    default_video_quality: str
    embed_metadata: bool
    embed_thumbnail: bool
    playlist_numbering: bool
    max_parallel_cap: int
    workers: int
    max_playlist_items: int
    downloads_dir: str
    timezone: str
    server_mode: bool = False
    admin_enabled: bool = False
    delivery_ttl_minutes: int = 0  # server mode: how long a prepared file / ZIP stays downloadable
    max_job_items: int = 0  # server mode: tracks per playlist / cart
    max_active_jobs: int = 0  # server mode: unfinished downloads per visitor
    max_subscriptions: int = 100  # followed channels / playlists (server mode: SERVER_MAX_SUBSCRIPTIONS)
    visitor_cookie_hours: int = 0  # server mode: how long a visitor's own cookies are kept
    connect_enabled: bool = False  # the extension sign-in is offered (admin switch)
    lyrics: bool = False  # a lyrics provider is configured (DL_LYRICS_URL)
    maintenance: bool = False  # server mode: new downloads are refused (SERVER_MAINTENANCE)
    maintenance_message: str = ""  # shown to visitors while `maintenance` is on


class ConnectUpload(BaseModel):
    """Body of ``POST /connect/{code}``: the cookies the extension read from the signed-in browser."""

    text: str = Field(max_length=600_000)


class CookieUpload(BaseModel):
    """Body of ``PUT /cookies``: the text of a Netscape cookies.txt."""

    text: str = Field(max_length=600_000)


class CartResponse(BaseModel):
    """Result of ``POST /api/cart/checkout``."""

    job: JobView
    rejected: list[RejectedUrl] = Field(default_factory=list)


class AdminLogin(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class BanRequest(BaseModel):
    ip: str = Field(min_length=2, max_length=64)
    reason: str = Field(default="", max_length=200)


class MkdirRequest(BaseModel):
    path: str = Field(min_length=1, max_length=300)


class FileOpRequest(BaseModel):
    """Admin file manager batch: move / copy into ``dest``, or delete."""

    paths: list[str] = Field(min_length=1, max_length=2000)
    dest: str | None = Field(default=None, max_length=300)


class ConfigKnob(BaseModel):
    """One live setting in the admin panel."""

    key: str
    group: str
    label: str
    help: str = ""
    kind: str  # int | bool | choice | speed | text
    min: int | None = None
    max: int | None = None
    choices: list[str] = Field(default_factory=list)
    server_only: bool = False
    value: Any = None
    default: Any = None
    overridden: bool = False


class AdminConfigUpdate(BaseModel):
    """``PUT /api/admin/config``."""

    values: dict[str, Any] = Field(default_factory=dict)
    reset: list[str] = Field(default_factory=list)


class AdminStatus(BaseModel):
    server_mode: bool
    enabled: bool  # an admin password is configured
    admin: bool  # this browser may use the dashboard (signed in, or personal mode without a password)
    username: str | None = None
