"""URL preview: title, thumbnail, qualities, playlist entries."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import Owner, rate
from api.schemas import InfoRequest
from models import EntryInfo, MediaInfo
from services import analytics, cookie_service, metadata_service

router = APIRouter(tags=["info"])


@router.post("/info", response_model=MediaInfo, dependencies=[Depends(rate("lookup"))])
def media_info(body: InfoRequest, who: Owner) -> MediaInfo:
    """
    Inspect a URL without downloading anything.

    Sync on purpose: yt-dlp is blocking, so FastAPI runs this in its threadpool.
    """
    analytics.count("previews")
    with cookie_service.scope(who):
        return metadata_service.preview(body.url, body.playlist_mode)


@router.get("/search", response_model=list[EntryInfo], dependencies=[Depends(rate("lookup"))])
def search(
    q: str = Query(..., min_length=2, max_length=200, description="Free-text search"),
    limit: int = Query(10, ge=1, le=25),
    music: bool = False,
    who: Owner = None,
) -> list[EntryInfo]:
    """Search YouTube by text; each result carries a URL ready for ``POST /api/downloads``."""
    analytics.search(q)
    with cookie_service.scope(who):
        return metadata_service.search(q, limit, music=music)
