"""Jobs, items, the download cart and queue-wide actions.

Every endpoint is scoped to the caller's own queue in server mode (``Owner``):
another visitor's job or item simply does not exist (404).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, StreamingResponse

from api.deps import Owner, attachment, rate
from api.schemas import (
    ActionResponse,
    BulkJobsRequest,
    BulkJobsResult,
    CartResponse,
    RejectedUrl,
    SubmitResponse,
)
from models import CartRequest, DownloadItem, DownloadRequest, DownloadStatus, JobView
from services import download_manager

router = APIRouter(tags=["downloads"])


@router.post("/downloads", response_model=SubmitResponse, status_code=201, dependencies=[Depends(rate("submit"))])
def create_downloads(body: DownloadRequest, who: Owner) -> SubmitResponse:
    """Queue one job per URL. Bad URLs are reported in ``rejected`` (HTTP 201 if any job was created)."""
    jobs, rejected = download_manager.submit(body, owner=who)
    return SubmitResponse(jobs=jobs, rejected=[RejectedUrl(**r) for r in rejected])


@router.post("/cart/checkout", response_model=CartResponse, status_code=201, dependencies=[Depends(rate("submit"))])
def checkout_cart(body: CartRequest, who: Owner) -> CartResponse:
    """Download the whole cart as ONE job (one folder; a ZIP in server mode or with ``zip=true``)."""
    job, rejected = download_manager.checkout_cart(body, owner=who)
    return CartResponse(job=job, rejected=[RejectedUrl(**r) for r in rejected])


@router.get("/downloads", response_model=list[JobView])
def list_downloads(
    who: Owner,
    status: DownloadStatus | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_items: bool = False,
) -> list[JobView]:
    """Newest-first job list; ``status`` filters on the derived job status."""
    return download_manager.list_job_views(
        status=status, limit=limit, offset=offset, include_items=include_items, owner=who
    )


@router.get("/downloads/{job_id}", response_model=JobView)
def get_download(job_id: str, who: Owner) -> JobView:
    """One job with all its items."""
    return download_manager.get_job_view(job_id, include_items=True, owner=who)


@router.get("/downloads/{job_id}/file")
def download_job_file(job_id: str, who: Owner):
    """
    Save a finished job to your device: the file of a single video, or a ZIP of a playlist / cart.
    Server mode serves the prepared file (Range-capable, resumable; 409 while it is being prepared).
    """
    name, payload = download_manager.job_file(job_id, owner=who)
    if isinstance(payload, Path):
        return FileResponse(payload, filename=name, content_disposition_type="attachment")
    return StreamingResponse(payload, media_type="application/zip", headers={"Content-Disposition": attachment(name)})


@router.post("/downloads/{job_id}/pause", response_model=JobView)
def pause_download(job_id: str, who: Owner) -> JobView:
    return download_manager.pause_job(job_id, owner=who)


@router.post("/downloads/{job_id}/resume", response_model=JobView)
def resume_download(job_id: str, who: Owner) -> JobView:
    return download_manager.resume_job(job_id, owner=who)


@router.post("/downloads/{job_id}/cancel", response_model=JobView)
def cancel_download(job_id: str, who: Owner) -> JobView:
    return download_manager.cancel_job(job_id, owner=who)


@router.post("/downloads/{job_id}/retry", response_model=JobView)
def retry_download(job_id: str, who: Owner) -> JobView:
    return download_manager.retry_job(job_id, owner=who)


@router.post("/downloads/{job_id}/restart", response_model=JobView)
def restart_download(job_id: str, who: Owner) -> JobView:
    """Start over from scratch — discards partial files and *replaces* finished ones."""
    return download_manager.restart_job(job_id, owner=who)


@router.post("/downloads/bulk", response_model=BulkJobsResult)
def bulk_downloads(body: BulkJobsRequest, who: Owner) -> BulkJobsResult:
    """Apply one action to many jobs (the UI's multi-select bar)."""
    return BulkJobsResult(**download_manager.bulk_jobs(body.ids, body.action, owner=who))


@router.delete("/downloads/{job_id}", response_model=ActionResponse)
def delete_download(job_id: str, who: Owner, delete_files: bool = False) -> ActionResponse:
    """Remove a job's records; ``delete_files=true`` also deletes the downloaded files (server mode: always)."""
    res = download_manager.delete_job(job_id, delete_files=delete_files, owner=who)
    return ActionResponse(affected=res["items"], message=f"{res['files']} file(s) deleted")


# ── single items ────────────────────────────────────────────────────


@router.get("/items/{item_id}", response_model=DownloadItem)
def get_item(item_id: str, who: Owner) -> DownloadItem:
    return download_manager.get_item(item_id, owner=who)


@router.get("/items/{item_id}/file")
def get_item_file(item_id: str, who: Owner, download: bool = False) -> FileResponse:
    """The finished file of one track (Range-capable: the player can seek). ``download=true`` → Save-As."""
    f = download_manager.item_file(item_id, owner=who)
    return FileResponse(f, filename=f.name, content_disposition_type="attachment" if download else "inline")


@router.post("/items/{item_id}/{action}", response_model=DownloadItem)
def item_action(
    item_id: str, action: Literal["pause", "resume", "cancel", "retry", "restart"], who: Owner
) -> DownloadItem:
    """pause | resume | cancel | retry | restart one item (409 when the state does not allow it)."""
    fn = {
        "pause": download_manager.pause_item,
        "resume": download_manager.resume_item,
        "cancel": download_manager.cancel_item,
        "retry": download_manager.retry_item,
        "restart": download_manager.restart_item,
    }[action]
    return fn(item_id, owner=who)


# ── whole queue ─────────────────────────────────────────────────────


@router.post("/queue/{action}", response_model=ActionResponse)
def queue_action(
    action: Literal["pause-all", "resume-all", "cancel-all", "retry-failed", "clear-finished"],
    who: Owner,
    delete_files: bool = False,
) -> ActionResponse:
    """Bulk actions across every job (server mode: every job of this visitor)."""
    if action == "pause-all":
        n = download_manager.pause_all(owner=who)
    elif action == "resume-all":
        n = download_manager.resume_all(owner=who)
    elif action == "cancel-all":
        n = download_manager.cancel_all(owner=who)
    elif action == "retry-failed":
        n = download_manager.retry_failed(owner=who)
    else:
        n = download_manager.clear_finished(delete_files=delete_files, owner=who)
    return ActionResponse(affected=n, message=f"{action}: {n} affected")
