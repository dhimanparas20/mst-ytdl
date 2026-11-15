"""
FastAPI application.

Run:  uv run uvicorn api.main:app --reload      (or: uv run python main.py api)
The Huey consumer is a SEPARATE process:  uv run python main.py worker
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import tasks  # noqa: F401  — registers the Huey dispatcher with services
from api.deps import visitor_middleware
from api.routers import admin, cookies, downloads, events, info, library, lyrics, stream, system
from configs import BASE_DIR, get_settings
from modules import attach_server_logs, get_logger, log_dir
from modules.redis_util import validate_redis_connection
from services import (
    ConflictError,
    ForbiddenError,
    InvalidRequestError,
    NotFoundError,
    RateLimitedError,
    ServiceError,
    UnavailableError,
    UpstreamError,
    access_service,
    admin_config,
    download_manager,
)

logger = get_logger(__name__)
WEB_DIR = BASE_DIR / "web"


class RevalidatingStaticFiles(StaticFiles):
    """Static files that browsers must revalidate (ETag → cheap 304) on every load,
    so UI edits show up immediately instead of after heuristic cache expiry."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: verify Redis, make sure the downloads dir exists, heal stale records."""
    s = get_settings()
    attach_server_logs()  # uvicorn + access lines into logs/ (uvicorn has configured its loggers by now)
    s.download.downloads_dir.mkdir(parents=True, exist_ok=True)
    try:
        admin_config.start_watcher()  # live settings from the admin panel (shared with the worker)
        validate_redis_connection(s.redis.redis_url)
        repaired = download_manager.reconcile()
        mode = "SERVER MODE (per-visitor queues, deliveries expire)" if s.server.mode else "personal mode"
        logger.info(f"ready — {mode}; downloads in {s.download.downloads_dir} ({repaired} record(s) repaired); logs in {log_dir()}")
        if s.server.mode and not access_service.admin_enabled:
            logger.warning("SERVER_MODE without a valid SERVER_ADMIN_PASSWORD_HASH: library management stays locked")
    except Exception as exc:
        logger.error(f"Redis unavailable at startup: {exc}")
    yield


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="mst-ytdl",
        description="YouTube / YouTube Music downloader — FastAPI + Huey + yt-dlp",
        version="0.1.0",
        debug=s.api.debug,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.api.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── errors: services raise, the API translates ──────────────────
    def _handler(status: int):
        async def handle(_: Request, exc: Exception) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=status)

        return handle

    app.add_exception_handler(NotFoundError, _handler(404))
    app.add_exception_handler(ConflictError, _handler(409))
    app.add_exception_handler(InvalidRequestError, _handler(422))
    app.add_exception_handler(UpstreamError, _handler(502))
    app.add_exception_handler(ForbiddenError, _handler(403))
    app.add_exception_handler(RateLimitedError, _handler(429))
    app.add_exception_handler(UnavailableError, _handler(503))
    app.add_exception_handler(ServiceError, _handler(400))

    async def redis_down(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": f"Redis unavailable: {exc}"}, status_code=503)

    app.add_exception_handler(redis.exceptions.ConnectionError, redis_down)
    app.add_exception_handler(redis.exceptions.TimeoutError, redis_down)

    # ── API ─────────────────────────────────────────────────────────
    if s.server.mode:  # every browser gets an anonymous id that scopes its queue
        app.middleware("http")(visitor_middleware)

    for r in (system.router, info.router, downloads.router, events.router, library.router, stream.router, cookies.router, admin.router, lyrics.router):
        app.include_router(r, prefix="/api")

    # ── frontend ────────────────────────────────────────────────────
    static_dir = WEB_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", RevalidatingStaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        page = WEB_DIR / "index.html"
        if not page.is_file():
            return JSONResponse({"detail": "UI not built yet — API docs at /docs"}, status_code=200)
        return FileResponse(page, headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
