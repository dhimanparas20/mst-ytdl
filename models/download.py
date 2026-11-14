"""
Download domain models — requests, persisted records, API views.

Pure data + validation (pydantic only): no redis / yt-dlp / settings imports.

Records persisted in Redis:
    DownloadJob   one user request (a single video or a whole playlist)
    DownloadItem  one media file inside a job (a single job has exactly one)

API views built from them:
    JobView       job + derived status/aggregates (+ optionally its items)
    MediaInfo     URL preview (what ``/api/info`` returns)
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from models.enums import (
    AUDIO_BITRATES,
    VIDEO_HEIGHTS,
    ControlAction,
    DownloadStatus,
    JobKind,
    MediaFormat,
    PlaylistMode,
    ResolveState,
)

# ──────────────────────────────────────────────
# Requests
# ──────────────────────────────────────────────


class DownloadOptions(BaseModel):
    """Fully-resolved per-job options (defaults already applied)."""

    format: MediaFormat
    quality: str = "best"
    embed_metadata: bool = True
    embed_thumbnail: bool = True
    subtitles: bool = False  # video outputs only
    subtitle_langs: list[str] = Field(default_factory=lambda: ["en"])
    speed_limit: int | None = Field(default=None, ge=1)  # bytes/sec per download
    overwrite: bool = False  # re-download even if the output file already exists

    @field_validator("quality", mode="before")
    @classmethod
    def _norm_quality(cls, v) -> str:
        """Accept ``1080`` / ``"1080p"`` / ``"320k"`` / ``"Best"``."""
        s = str(v or "best").strip().lower()
        for suffix in ("kbps", "k", "p"):
            if s.endswith(suffix) and s[: -len(suffix)].isdigit():
                s = s[: -len(suffix)]
                break
        return s or "best"

    @model_validator(mode="after")
    def _check_quality(self) -> "DownloadOptions":
        """Quality must match the format family; lossless/opus ignore it."""
        fmt, q = self.format, self.quality
        if fmt.is_audio:
            if not fmt.lossy_bitrate:
                self.quality = "best"  # opus / flac / wav: nothing to choose
            elif q != "best" and q not in AUDIO_BITRATES:
                raise ValueError(
                    f"Audio quality must be 'best' or one of {', '.join(AUDIO_BITRATES)} kbps"
                )
        else:
            if q != "best" and q not in VIDEO_HEIGHTS:
                raise ValueError(
                    f"Video quality must be 'best' or one of {', '.join(VIDEO_HEIGHTS)} (p)"
                )
        if not fmt.is_video:
            self.subtitles = False
        self.subtitle_langs = [x.strip() for x in self.subtitle_langs if x.strip()] or ["en"]
        return self


class DownloadRequest(BaseModel):
    """Body of ``POST /api/downloads``. ``None`` options fall back to settings."""

    urls: list[str] = Field(min_length=1, max_length=50)
    format: MediaFormat | None = None
    quality: str | None = None
    playlist_mode: PlaylistMode = PlaylistMode.AUTO
    # 1-based selection inside a playlist: "1,3,5-7" (None = everything)
    playlist_items: str | None = Field(default=None, max_length=500)
    embed_metadata: bool | None = None
    embed_thumbnail: bool | None = None
    subtitles: bool = False
    subtitle_langs: list[str] = Field(default_factory=lambda: ["en"])
    speed_limit: int | None = Field(default=None, ge=1)
    overwrite: bool = False
    # Override the destination folder name (sanitised, relative to downloads/)
    folder: str | None = Field(default=None, max_length=120)

    @field_validator("urls", mode="before")
    @classmethod
    def _clean_urls(cls, v):
        """Trim, drop blanks and exact duplicates (order preserved)."""
        if isinstance(v, str):
            v = v.splitlines()
        seen: set[str] = set()
        out: list[str] = []
        for raw in v or []:
            u = str(raw).strip()
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        if not out:
            raise ValueError("Provide at least one URL")
        return out


# ──────────────────────────────────────────────
# Persisted records
# ──────────────────────────────────────────────


class DownloadJob(BaseModel):
    """One request: a single video or a playlist. Stored as a Redis hash."""

    job_id: str
    url: str
    kind: JobKind = JobKind.SINGLE
    title: str = ""
    uploader: str | None = None
    thumbnail: str | None = None
    options: DownloadOptions
    folder: str = ""  # relative to the downloads dir ('' until resolved)
    folder_override: str | None = None  # user-chosen folder name (playlists resolve later)
    playlist_items: str | None = None
    playlist_size: int = 0  # entries in the whole playlist (numbering width)
    resolve_state: ResolveState = ResolveState.READY
    resolve_error: str | None = None
    total_items: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0


class DownloadItem(BaseModel):
    """One media file. Stored as a Redis hash so workers patch single fields."""

    item_id: str
    job_id: str
    index: int = 1  # 1-based position in the playlist
    url: str
    video_id: str | None = None
    title: str = ""
    uploader: str | None = None
    thumbnail: str | None = None
    duration: float | None = None

    status: DownloadStatus = DownloadStatus.QUEUED
    control: ControlAction | None = None  # pending pause/cancel request
    token: str = ""  # run token; stale (re-queued) tasks see a mismatch and exit
    stage: str = ""  # human label: video / audio / merging / converting ...
    percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed: float | None = None  # bytes/sec
    eta: int | None = None  # seconds

    filepath: str | None = None  # relative to the downloads dir
    filesize: int | None = None
    partials: list[str] = Field(default_factory=list)  # leftover .part files (for cancel)
    force: bool = False  # restart/replace requested: ignore any existing output file
    error: str | None = None
    attempts: int = 0

    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None


# ──────────────────────────────────────────────
# API views
# ──────────────────────────────────────────────


def derive_job_status(
    resolve_state: ResolveState, resolve_error: str | None, items: list[DownloadItem]
) -> DownloadStatus:
    """
    Collapse item states (+ playlist expansion state) into one job status.

    Precedence: expanding > any running > any waiting (queued) > any paused >
    terminal roll-up (completed / partial / failed / cancelled).
    """
    if resolve_state in (ResolveState.PENDING, ResolveState.RESOLVING):
        return DownloadStatus.RESOLVING
    if not items:
        return DownloadStatus.FAILED if resolve_state is ResolveState.FAILED or resolve_error else DownloadStatus.QUEUED

    statuses = [i.status for i in items]
    n = len(statuses)
    done = statuses.count(DownloadStatus.COMPLETED)

    if any(s is DownloadStatus.DOWNLOADING for s in statuses):
        return DownloadStatus.DOWNLOADING
    if any(s is DownloadStatus.PROCESSING for s in statuses):
        return DownloadStatus.PROCESSING
    if any(s is DownloadStatus.QUEUED for s in statuses):
        # Between items of a playlist the job is still "in progress".
        return DownloadStatus.DOWNLOADING if done else DownloadStatus.QUEUED
    if any(s is DownloadStatus.PAUSED for s in statuses):
        return DownloadStatus.PAUSED

    failed = statuses.count(DownloadStatus.FAILED)
    if done == n:
        return DownloadStatus.COMPLETED
    if done == 0:
        return DownloadStatus.FAILED if failed else DownloadStatus.CANCELLED
    return DownloadStatus.PARTIAL


class JobView(BaseModel):
    """A job plus everything the UI needs to render it."""

    job_id: str
    url: str
    kind: JobKind
    title: str
    uploader: str | None = None
    thumbnail: str | None = None
    options: DownloadOptions
    folder: str
    status: DownloadStatus
    resolve_state: ResolveState
    resolve_error: str | None = None

    total_items: int
    counts: dict[str, int] = Field(default_factory=dict)  # status → n
    percent: float = 0.0
    speed: float = 0.0  # bytes/sec, sum of running items
    eta: int | None = None  # seconds, slowest running item
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    items: list[DownloadItem] | None = None

    @classmethod
    def build(
        cls, job: DownloadJob, items: list[DownloadItem], *, include_items: bool = False
    ) -> "JobView":
        """Aggregate ``items`` into a view of ``job``."""
        counts: dict[str, int] = {}
        for it in items:
            counts[it.status.value] = counts.get(it.status.value, 0) + 1

        live = [i for i in items if i.status is not DownloadStatus.CANCELLED]
        percent = (
            sum(100.0 if i.status is DownloadStatus.COMPLETED else i.percent for i in live) / len(live)
            if live
            else 0.0
        )
        running = [i for i in items if i.status.is_running]
        speed = sum(i.speed or 0.0 for i in running)
        etas = [i.eta for i in running if i.eta is not None]
        totals = [i.total_bytes for i in live if i.total_bytes]

        return cls(
            job_id=job.job_id,
            url=job.url,
            kind=job.kind,
            title=job.title or job.url,
            uploader=job.uploader,
            thumbnail=job.thumbnail,
            options=job.options,
            folder=job.folder,
            status=derive_job_status(job.resolve_state, job.resolve_error, items),
            resolve_state=job.resolve_state,
            resolve_error=job.resolve_error,
            total_items=max(job.total_items, len(items)),
            counts=counts,
            percent=round(percent, 1),
            speed=speed,
            eta=max(etas) if etas else None,
            downloaded_bytes=sum(i.downloaded_bytes for i in items),
            total_bytes=sum(totals) if totals else None,
            created_at=job.created_at,
            updated_at=max([job.updated_at, *(i.updated_at for i in items)]),
            items=items if include_items else None,
        )


class EntryInfo(BaseModel):
    """One video inside a previewed playlist."""

    index: int
    video_id: str | None = None
    url: str
    title: str = ""
    uploader: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    available: bool = True


class MediaInfo(BaseModel):
    """Preview of a URL (``POST /api/info``)."""

    url: str
    kind: JobKind
    title: str
    uploader: str | None = None
    thumbnail: str | None = None
    duration: float | None = None
    entry_count: int = 1
    entries: list[EntryInfo] = Field(default_factory=list)
    # Video heights actually offered (e.g. [360, 720, 1080]); empty for playlists
    available_heights: list[int] = Field(default_factory=list)
    # URL carries both a video id and a list id: the UI should let the user pick
    ambiguous: bool = False
    folder_preview: str = ""


class RuntimeSettings(BaseModel):
    """Settings the UI can change without a restart (stored in Redis)."""

    max_parallel: int = Field(ge=1)
    speed_limit: int | None = Field(default=None, ge=1)  # bytes/sec per download


class QueueStats(BaseModel):
    """Headline numbers for the dashboard."""

    total_jobs: int = 0
    total_items: int = 0
    active: int = 0  # downloading + processing
    queued: int = 0
    paused: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    speed: float = 0.0
    downloaded_bytes: int = 0
    max_parallel: int = 0
    workers: int = 0
