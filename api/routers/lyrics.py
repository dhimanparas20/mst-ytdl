"""Lyrics for the player (synced when the provider has them)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.deps import rate
from models import Lyrics
from services import analytics, lyrics_service
from services.lyrics import LYRIC_LANGS, normalize_lang

router = APIRouter(tags=["lyrics"])


@router.get("/lyrics", response_model=Lyrics, dependencies=[Depends(rate("lyrics"))])
def lyrics(
    title: str = Query(..., min_length=1, max_length=300),
    artist: str | None = Query(None, max_length=200),
    duration: float | None = Query(None, ge=0, le=36000),
    lang: str = Query("auto", max_length=12, description="auto = original; else a language code"),
) -> Lyrics:
    """Lyrics of a track by title / artist / duration (``found=false`` + ``message`` when there are none)."""
    analytics.count("lyrics")
    return lyrics_service.find(title, artist, duration, lang=normalize_lang(lang))


@router.get("/lyrics/languages")
def lyric_languages() -> dict[str, list[dict[str, str]]]:
    """Languages the player may request (``auto`` + translate targets)."""
    labels = {
        "auto": "Auto (original)",
        "en": "English", "es": "Spanish", "fr": "French", "de": "German",
        "pt": "Portuguese", "it": "Italian", "ja": "Japanese", "ko": "Korean",
        "zh-CN": "Chinese (Simplified)", "zh-TW": "Chinese (Traditional)",
        "hi": "Hindi", "ar": "Arabic", "ru": "Russian", "th": "Thai",
        "vi": "Vietnamese", "id": "Indonesian", "tr": "Turkish",
    }
    return {"languages": [{"code": c, "label": labels.get(c, c)} for c in LYRIC_LANGS]}
