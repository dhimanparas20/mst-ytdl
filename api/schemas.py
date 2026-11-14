"""Request / response bodies that only the HTTP layer needs."""

from __future__ import annotations

from typing import Literal

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
    max_parallel_cap: int
    workers: int
    max_playlist_items: int
    downloads_dir: str
    timezone: str
