"""URL preview: title, thumbnail, qualities, playlist entries."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import Owner, rate
from api.schemas import InfoRequest
from models import ExploreFeed, MediaInfo, SearchResults
from models.paging import SEARCH_DEFAULT_PAGE_SIZE, SEARCH_MAX_PAGE_SIZE, SEARCH_MAX_PAGES
from services import analytics, cookie_service, explore_service, metadata_service

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


@router.get("/search", response_model=SearchResults, dependencies=[Depends(rate("lookup"))])
def search(
    q: str = Query(..., min_length=2, max_length=200, description="Free-text search"),
    page: int = Query(1, ge=1, le=SEARCH_MAX_PAGES),
    limit: int = Query(SEARCH_DEFAULT_PAGE_SIZE, ge=1, le=SEARCH_MAX_PAGE_SIZE, description="Page size"),
    music: bool = False,
    who: Owner = None,
) -> SearchResults:
    """Search YouTube by text; paginated (max 10 pages). Each entry is ready for ``POST /api/downloads``."""
    analytics.search(q)
    with cookie_service.scope(who):
        return metadata_service.search_page(q, page=page, page_size=limit, music=music)


@router.get("/search/suggest", response_model=list[str], dependencies=[Depends(rate("lookup"))])
def search_suggest(
    q: str = Query("", max_length=200, description="Optional filter prefix / substring"),
    limit: int = Query(8, ge=1, le=20),
) -> list[str]:
    """Popular recent search queries for the omnibar (from analytics; may be empty on a fresh install)."""
    return analytics.suggest_queries(q, limit)


@router.get("/explore", response_model=ExploreFeed, dependencies=[Depends(rate("lookup"))])
def explore(
    kind: str = Query("auto", description="auto | music | videos | for_you"),
    page: int = Query(1, ge=1, le=SEARCH_MAX_PAGES),
    limit: int = Query(SEARCH_DEFAULT_PAGE_SIZE, ge=1, le=40, description="Page size"),
    who: Owner = None,
) -> ExploreFeed:
    """
    Home Explore shelf (Music / Videos / For you), paginated up to 10 pages.

    ``for_you`` needs login cookies. Music / Videos work anonymously via search (experimental in
    the UI — may fail when YouTube blocks cookie-less lookups). ``signed_in`` reflects the session.
    """
    return explore_service.get(who, kind, limit, page=page)
