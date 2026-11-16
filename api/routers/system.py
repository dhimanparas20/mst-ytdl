"""System endpoints: health, UI config, runtime settings."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from yt_dlp.version import __version__ as ytdlp_version

from api.deps import admin_only
from api.schemas import ConfigResponse, HealthResponse, RuntimeUpdate
from configs import get_ffmpeg_path, get_js_runtimes, get_redis, get_settings
from models import (
    AUDIO_BITRATES,
    AUDIO_FORMATS,
    VIDEO_FORMATS,
    VIDEO_HEIGHTS,
    RuntimeSettings,
)
from services import access_service, runtime_service, subscription_service

router = APIRouter(tags=["system"])


def _downloads_dir_label() -> str:
    """Absolute path in personal mode; just the folder name for the public (server mode)."""
    d = get_settings().download.downloads_dir
    return d.name if get_settings().server.mode else str(d)


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness + dependency check (Redis, worker heartbeat, ffmpeg, JS runtime)."""
    from tasks import heartbeat_key  # local import: keeps the module graph light

    detail: list[str] = []
    redis_ok = worker_ok = False
    try:
        r = get_redis()
        redis_ok = bool(r.ping())
        worker_ok = bool(r.exists(heartbeat_key()))
    except Exception as exc:  # redis down
        detail.append(f"redis: {exc}")
    if redis_ok and not worker_ok:
        detail.append("no Huey consumer seen — start one: uv run python main.py worker")

    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        detail.append("ffmpeg not found — conversions/merging will fail")
    js = get_js_runtimes()
    js_name = next(iter(js)) if js else None
    if not js_name:
        detail.append("no JS runtime (node/deno/bun) — YouTube downloads/playback will fail for many formats")

    return HealthResponse(
        status="ok" if redis_ok and worker_ok and ffmpeg and js_name else "degraded",
        redis=redis_ok,
        worker=worker_ok,
        ffmpeg=ffmpeg,
        js_runtime=js_name,
        yt_dlp=ytdlp_version,
        downloads_dir=_downloads_dir_label(),
        detail=detail,
    )


@router.get("/config", response_model=ConfigResponse)
def config() -> ConfigResponse:
    """Option lists and defaults for the UI pickers."""
    s = get_settings()
    return ConfigResponse(
        audio_formats=sorted(f.value for f in AUDIO_FORMATS),
        video_formats=sorted(f.value for f in VIDEO_FORMATS),
        audio_bitrates=list(AUDIO_BITRATES),
        video_heights=list(VIDEO_HEIGHTS),
        default_format=s.download.default_format,
        default_audio_quality=s.download.default_audio_quality,
        default_video_quality=s.download.default_video_quality,
        embed_metadata=s.download.embed_metadata,
        embed_thumbnail=s.download.embed_thumbnail,
        playlist_numbering=s.download.playlist_numbering,
        max_parallel_cap=s.task.max_parallel_cap,
        workers=s.task.workers,
        max_playlist_items=s.download.max_playlist_items,
        downloads_dir=_downloads_dir_label(),
        timezone=s.download.timezone,
        server_mode=s.server.mode,
        admin_enabled=access_service.admin_enabled,
        delivery_ttl_minutes=s.server.delivery_ttl_minutes if s.server.mode else 0,
        max_job_items=s.server.max_job_items if s.server.mode else 0,
        max_active_jobs=s.server.max_active_jobs if s.server.mode else 0,
        max_subscriptions=subscription_service.limit(),
        visitor_cookie_hours=s.server.visitor_cookie_hours if s.server.mode else 0,
        lyrics=bool(s.download.lyrics_url),
        connect_enabled=s.server.connect_enabled,
        maintenance=s.server.mode and s.server.maintenance,
        maintenance_message=s.server.maintenance_message if s.server.mode and s.server.maintenance else "",
    )


@router.get("/runtime", response_model=RuntimeSettings)
def get_runtime() -> RuntimeSettings:
    """Current parallelism / speed-limit settings."""
    return runtime_service.get()


@router.put("/runtime", response_model=RuntimeSettings, dependencies=[Depends(admin_only)])
def put_runtime(body: RuntimeUpdate) -> RuntimeSettings:
    """Change parallel limit and/or speed cap without a restart."""
    kwargs: dict = {}
    if "max_parallel" in body.model_fields_set and body.max_parallel is not None:
        kwargs["max_parallel"] = body.max_parallel
    if "speed_limit" in body.model_fields_set:
        kwargs["speed_limit"] = body.speed_limit  # None / 0 → unlimited
    if "default_folder" in body.model_fields_set:
        kwargs["default_folder"] = body.default_folder  # None / "" → no base folder
    return runtime_service.update(**kwargs)
