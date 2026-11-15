"""Lyrics for the player (synced when the provider has them)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import rate
from models import Lyrics
from services import analytics, lyrics_service

router = APIRouter(tags=["lyrics"])


@router.get("/lyrics", response_model=Lyrics, dependencies=[Depends(rate("lyrics"))])
def lyrics(
    title: str = Query(..., min_length=1, max_length=300),
    artist: str | None = Query(None, max_length=200),
    duration: float | None = Query(None, ge=0, le=36000),
) -> Lyrics:
    """Lyrics of a track by title / artist / duration (``found=false`` + ``message`` when there are none)."""
    analytics.count("lyrics")
    return lyrics_service.find(title, artist, duration)
