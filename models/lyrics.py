"""Lyrics of a track (``GET /api/lyrics``): time-synced lines when available, else plain text."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LyricLine(BaseModel):
    """One synced line: starts at ``t`` seconds."""

    t: float = Field(ge=0)
    text: str


class Lyrics(BaseModel):
    """What the lyrics provider knows about a track."""

    found: bool = False
    track: str = ""
    artist: str = ""
    synced: list[LyricLine] = Field(default_factory=list)  # empty → only plain text (or nothing)
    plain: str | None = None
    instrumental: bool = False
    source: str = ""  # provider name, shown as attribution
    message: str = ""  # why nothing was found (provider down, disabled …)
