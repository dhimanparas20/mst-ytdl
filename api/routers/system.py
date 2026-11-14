"""System endpoints: health, UI config, runtime settings."""

from __future__ import annotations

from fastapi import APIRouter
from yt_dlp.version import __version__ as ytdlp_version

from api.schemas import ConfigResponse, HealthResponse, RuntimeUpdate
from configs import get_ffmpeg_path, get_js_runtimes, get_redis, get_settings
from models import (
    AUDIO_BITRATES,
    AUDIO_FORMATS,
    VIDEO_FORMATS,
    VIDEO_HEIGHTS,
    RuntimeSettings,
)
from services import runtime_service

router = APIRouter(tags=["system"])


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
        detail.append("no JS runtime (node/deno/bun) — some YouTube formats may be missing")

    return HealthResponse(
        status="ok" if redis_ok and worker_ok and ffmpeg else "degraded",
        redis=redis_ok,
        worker=worker_ok,
        ffmpeg=ffmpeg,
        js_runtime=js_name,
        yt_dlp=ytdlp_version,
        downloads_dir=str(get_settings().download.downloads_dir),
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
        downloads_dir=str(s.download.downloads_dir),
        timezone=s.download.timezone,
    )


@router.get("/runtime", response_model=RuntimeSettings)
def get_runtime() -> RuntimeSettings:
    """Current parallelism / speed-limit settings."""
    return runtime_service.get()


@router.put("/runtime", response_model=RuntimeSettings)
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
