"""Home-page explore shelf: personalized feed or public trending music / videos."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from models.download import EntryInfo

ExploreSource = Literal["for_you", "trending_music", "trending_videos"]


class ExploreFeed(BaseModel):
    """What the Home page shows in the Explore shelf (paginated)."""

    source: ExploreSource
    title: str
    signed_in: bool = False
    entries: list[EntryInfo] = Field(default_factory=list)
    message: str | None = None
    page: int = 1
    page_size: int = 12
    pages: int = 1
