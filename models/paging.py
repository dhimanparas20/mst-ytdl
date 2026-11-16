"""Paged result envelopes for search and explore shelves."""

from __future__ import annotations

from pydantic import BaseModel, Field

from models.download import EntryInfo

# Soft caps shared by API + services (yt-dlp ytsearchN has no real offset).
SEARCH_MAX_PAGES = 10
SEARCH_DEFAULT_PAGE_SIZE = 12
SEARCH_MAX_PAGE_SIZE = 25
SEARCH_POOL_CAP = SEARCH_MAX_PAGES * SEARCH_DEFAULT_PAGE_SIZE  # 120


class SearchResults(BaseModel):
    """One page of YouTube text-search hits."""

    entries: list[EntryInfo] = Field(default_factory=list)
    page: int = 1
    page_size: int = SEARCH_DEFAULT_PAGE_SIZE
    pages: int = 1


def page_window(total: int, page: int, page_size: int, *, max_pages: int = SEARCH_MAX_PAGES) -> tuple[int, int, int]:
    """
    Clamp page / derive ``pages`` from a known pool length.

    Returns:
        (page, pages, start_index) — ``pages`` is at least 1.
    """
    size = max(1, page_size)
    cap = max(1, max_pages)
    if total <= 0:
        return 1, 1, 0
    pages = min(cap, max(1, (total + size - 1) // size))
    p = max(1, min(int(page), pages))
    return p, pages, (p - 1) * size
