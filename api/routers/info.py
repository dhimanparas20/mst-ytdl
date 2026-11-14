"""URL preview: title, thumbnail, qualities, playlist entries."""

from __future__ import annotations

from fastapi import APIRouter, Query

from api.schemas import InfoRequest
from models import EntryInfo, MediaInfo
from services import metadata_service

router = APIRouter(tags=["info"])


@router.post("/info", response_model=MediaInfo)
def media_info(body: InfoRequest) -> MediaInfo:
    """
    Inspect a URL without downloading anything.

    Sync on purpose: yt-dlp is blocking, so FastAPI runs this in its threadpool.
    """
    return metadata_service.preview(body.url, body.playlist_mode)


@router.get("/search", response_model=list[EntryInfo])
def search(
    q: str = Query(..., min_length=2, max_length=200, description="Free-text search"),
    limit: int = Query(10, ge=1, le=25),
    music: bool = False,
) -> list[EntryInfo]:
    """Search YouTube by text; each result carries a URL ready for ``POST /api/downloads``."""
    return metadata_service.search(q, limit, music=music)
