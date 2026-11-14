"""Jobs, items and queue-wide actions."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from api.schemas import ActionResponse, BulkJobsRequest, BulkJobsResult, RejectedUrl, SubmitResponse
from models import DownloadItem, DownloadRequest, DownloadStatus, JobView
from services import download_manager

router = APIRouter(tags=["downloads"])


@router.post("/downloads", response_model=SubmitResponse, status_code=201)
def create_downloads(body: DownloadRequest) -> SubmitResponse:
    """Queue one job per URL. Bad URLs are reported in ``rejected`` (HTTP 201 if any job was created)."""
    jobs, rejected = download_manager.submit(body)
    return SubmitResponse(jobs=jobs, rejected=[RejectedUrl(**r) for r in rejected])


@router.get("/downloads", response_model=list[JobView])
def list_downloads(
    status: DownloadStatus | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_items: bool = False,
) -> list[JobView]:
    """Newest-first job list; ``status`` filters on the derived job status."""
    return download_manager.list_job_views(
        status=status, limit=limit, offset=offset, include_items=include_items
    )


@router.get("/downloads/{job_id}", response_model=JobView)
def get_download(job_id: str) -> JobView:
    """One job with all its items."""
    return download_manager.get_job_view(job_id, include_items=True)


@router.post("/downloads/{job_id}/pause", response_model=JobView)
def pause_download(job_id: str) -> JobView:
    return download_manager.pause_job(job_id)


@router.post("/downloads/{job_id}/resume", response_model=JobView)
def resume_download(job_id: str) -> JobView:
    return download_manager.resume_job(job_id)


@router.post("/downloads/{job_id}/cancel", response_model=JobView)
def cancel_download(job_id: str) -> JobView:
    return download_manager.cancel_job(job_id)


@router.post("/downloads/{job_id}/retry", response_model=JobView)
def retry_download(job_id: str) -> JobView:
    return download_manager.retry_job(job_id)


@router.post("/downloads/{job_id}/restart", response_model=JobView)
def restart_download(job_id: str) -> JobView:
    """Start over from scratch — discards partial files and *replaces* finished ones."""
    return download_manager.restart_job(job_id)


@router.post("/downloads/bulk", response_model=BulkJobsResult)
def bulk_downloads(body: BulkJobsRequest) -> BulkJobsResult:
    """Apply one action to many jobs (the UI's multi-select bar)."""
    return BulkJobsResult(**download_manager.bulk_jobs(body.ids, body.action))


@router.delete("/downloads/{job_id}", response_model=ActionResponse)
def delete_download(job_id: str, delete_files: bool = False) -> ActionResponse:
    """Remove a job's records; ``delete_files=true`` also deletes the downloaded files."""
    res = download_manager.delete_job(job_id, delete_files=delete_files)
    return ActionResponse(affected=res["items"], message=f"{res['files']} file(s) deleted")


# ── single items ────────────────────────────────────────────────────


@router.get("/items/{item_id}", response_model=DownloadItem)
def get_item(item_id: str) -> DownloadItem:
    return download_manager.get_item(item_id)


@router.post("/items/{item_id}/{action}", response_model=DownloadItem)
def item_action(
    item_id: str, action: Literal["pause", "resume", "cancel", "retry", "restart"]
) -> DownloadItem:
    """pause | resume | cancel | retry | restart one item (409 when the state does not allow it)."""
    fn = {
        "pause": download_manager.pause_item,
        "resume": download_manager.resume_item,
        "cancel": download_manager.cancel_item,
        "retry": download_manager.retry_item,
        "restart": download_manager.restart_item,
    }[action]
    return fn(item_id)


# ── whole queue ─────────────────────────────────────────────────────


@router.post("/queue/{action}", response_model=ActionResponse)
def queue_action(
    action: Literal["pause-all", "resume-all", "cancel-all", "retry-failed", "clear-finished"],
    delete_files: bool = False,
) -> ActionResponse:
    """Bulk actions across every job."""
    if action == "pause-all":
        n = download_manager.pause_all()
    elif action == "resume-all":
        n = download_manager.resume_all()
    elif action == "cancel-all":
        n = download_manager.cancel_all()
    elif action == "retry-failed":
        n = download_manager.retry_failed()
    else:
        n = download_manager.clear_finished(delete_files=delete_files)
    return ActionResponse(affected=n, message=f"{action}: {n} affected")
