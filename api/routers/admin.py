"""
The /admin dashboard API: sign-in, analytics, activity, visitors and bans, live settings,
the file manager, maintenance and logs.

Everything except ``GET /admin`` and sign-in / sign-out needs an admin session (``superadmin``):
a configured password always does; personal mode without one is open (your own machine).
The page itself is served at ``/admin`` by ``api.main``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from api.deps import admin_token, attachment, client_ip, is_https, superadmin
from api.schemas import (
    ActionResponse,
    AdminConfigUpdate,
    AdminLogin,
    AdminStatus,
    BanRequest,
    BulkJobsRequest,
    BulkJobsResult,
    ConfigKnob,
    FileOpRequest,
    MkdirRequest,
    RenameRequest,
)
from models import DownloadStatus, JobView
from services import (
    FileEntry,
    access_service,
    admin_config,
    analytics,
    download_manager,
    file_manager,
    maintenance,
)
from services.access import ADMIN_COOKIE

router = APIRouter(prefix="/admin", tags=["admin"])
guarded = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(superadmin)])


def _status(token: str | None) -> AdminStatus:
    enabled = access_service.admin_enabled
    signed_in = access_service.is_admin(token)
    return AdminStatus(
        server_mode=access_service.server_mode,
        enabled=enabled,
        admin=signed_in or (not enabled and not access_service.server_mode),
        username=access_service.username if signed_in else None,
    )


# ── session ─────────────────────────────────────────────────────────


@router.get("", response_model=AdminStatus)
def admin_status(request: Request) -> AdminStatus:
    """Is admin sign-in available, and is this browser signed in?"""
    return _status(admin_token(request))


@router.post("/login", response_model=AdminStatus)
def admin_login(body: AdminLogin, request: Request, response: Response) -> AdminStatus:
    """Username + password → an HttpOnly session cookie (403 wrong, 429 locked out)."""
    token = access_service.login(body.username, body.password, client_ip(request))
    response.set_cookie(
        ADMIN_COOKIE, token, max_age=access_service.session_seconds, httponly=True, samesite="strict",
        secure=is_https(request), path="/",
    )
    return _status(token)


@router.post("/logout", response_model=AdminStatus)
def admin_logout(request: Request, response: Response) -> AdminStatus:
    """End the admin session."""
    access_service.logout(admin_token(request))
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return _status(None)


# ── overview / activity ─────────────────────────────────────────────


@guarded.get("/overview")
def admin_overview() -> dict:
    """KPIs, health, storage, limits and the statistics series of the dashboard."""
    return maintenance.overview()


@guarded.get("/jobs", response_model=list[JobView])
def admin_jobs(status: DownloadStatus | None = None, limit: int = Query(200, ge=1, le=1000)) -> list[JobView]:
    """Jobs of every visitor, newest first (``owner`` tells whose)."""
    return download_manager.list_job_views(status=status, limit=limit, owner=None)


@guarded.post("/jobs/bulk", response_model=BulkJobsResult)
def admin_jobs_bulk(body: BulkJobsRequest) -> BulkJobsResult:
    """Pause / resume / cancel / retry / restart / remove any jobs."""
    return BulkJobsResult(**download_manager.bulk_jobs(body.ids, body.action, owner=None))


@guarded.get("/visitors")
def admin_visitors(limit: int = Query(200, ge=1, le=2000)) -> list[dict]:
    """Recently seen visitors (server mode): address, browser, requests, online now."""
    return analytics.visitors(limit)


@guarded.get("/bans")
def admin_bans() -> list[dict]:
    return access_service.bans()


@guarded.post("/bans", response_model=ActionResponse)
def admin_ban(body: BanRequest) -> ActionResponse:
    """Block an address (server mode: every request from it gets 403)."""
    access_service.ban(body.ip.strip(), body.reason)
    return ActionResponse(affected=1, message=f"{body.ip} blocked")


@guarded.delete("/bans/{ip}", response_model=ActionResponse)
def admin_unban(ip: str) -> ActionResponse:
    access_service.unban(ip)
    return ActionResponse(affected=1, message=f"{ip} unblocked")


# ── live settings ───────────────────────────────────────────────────


@guarded.get("/config", response_model=list[ConfigKnob])
def admin_config_list() -> list[ConfigKnob]:
    """Every setting the admin may change live, with its value, .env default and override flag."""
    return [ConfigKnob(**k) for k in admin_config.list()]


@guarded.put("/config", response_model=list[ConfigKnob])
def admin_config_update(body: AdminConfigUpdate) -> list[ConfigKnob]:
    """Change settings (validated all-or-nothing); ``reset`` puts keys back to their .env value."""
    return [ConfigKnob(**k) for k in admin_config.update(body.values, body.reset)]


# ── file manager ────────────────────────────────────────────────────


@guarded.get("/files", response_model=list[FileEntry])
def admin_files(path: str = "", hidden: bool = True) -> list[FileEntry]:
    """A folder of the downloads root (hidden entries included unless ``hidden=false``)."""
    return file_manager.list(path, hidden)


@guarded.get("/files/usage")
def admin_files_usage() -> list[dict]:
    """Size of every top-level entry — what fills the disk."""
    return file_manager.tree_usage()


@guarded.post("/files/mkdir", response_model=FileEntry)
def admin_mkdir(body: MkdirRequest) -> FileEntry:
    return file_manager.mkdir(body.path)


@guarded.post("/files/rename", response_model=FileEntry)
def admin_rename(body: RenameRequest) -> FileEntry:
    return file_manager.rename(body.path, body.name)


@guarded.post("/files/{op}")
def admin_file_op(op: Literal["move", "copy", "delete"], body: FileOpRequest) -> dict:
    """Batch move / copy (into ``dest``, created if missing) or delete; per-path errors are listed."""
    if op == "delete":
        return file_manager.delete(body.paths)
    return (file_manager.move if op == "move" else file_manager.copy)(body.paths, body.dest or "")


@guarded.get("/files/download")
def admin_file_download(path: str, inline: bool = False) -> FileResponse:
    """A file (Range-capable; ``inline=true`` plays / shows it in the browser)."""
    f = file_manager.file(path)
    return FileResponse(f, filename=f.name, content_disposition_type="inline" if inline else "attachment")


@guarded.get("/files/zip")
def admin_file_zip(path: Annotated[list[str], Query()]) -> StreamingResponse:
    name, chunks = file_manager.zip(path)
    return StreamingResponse(chunks, media_type="application/zip", headers={"Content-Disposition": attachment(name)})


@guarded.get("/files/text", response_class=PlainTextResponse)
def admin_file_text(path: str) -> str:
    """First 256 KB of a text file (logs, subtitles, lyrics, JSON)."""
    return file_manager.text(path)


# ── maintenance / logs ──────────────────────────────────────────────


@guarded.get("/maintenance")
def admin_maintenance_actions() -> list[dict]:
    return [{"action": k, "label": v[0], "help": v[1]} for k, v in maintenance.actions.items()]


@guarded.post("/maintenance/{action}")
def admin_maintenance_run(action: str) -> dict:
    return maintenance.run(action)


@guarded.get("/logs")
def admin_logs() -> list[dict]:
    return maintenance.log_files()


@guarded.get("/logs/{name}")
def admin_log_tail(
    name: str, lines: int = Query(300, ge=10, le=2000), level: str | None = None, q: str | None = Query(None, max_length=200)
) -> dict:
    return maintenance.tail(name, lines, level, q)
