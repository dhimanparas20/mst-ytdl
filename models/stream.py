"""
Streaming models — what the in-app player needs to play a YouTube video through our server.

Pure data (pydantic only). Built by ``services.streaming.StreamService``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StreamFormat(BaseModel):
    """One directly-playable HTTPS stream of a video."""

    format_id: str
    kind: Literal["muxed", "video", "audio"]  # muxed = picture + sound in one file
    label: str  # "720p", "1080p60", "128 kbps m4a"
    height: int | None = None
    fps: float | None = None
    ext: str = ""
    vcodec: str | None = None
    acodec: str | None = None
    abr: float | None = None
    filesize: int | None = None


class QualityOption(BaseModel):
    """
    One entry of the player's quality menu.

    ``muxed`` options play from a single file. Otherwise the picture comes from
    ``video_format`` and the sound from ``audio_format`` (two synchronised elements) —
    that is how every resolution above YouTube's progressive 360p/720p is reachable.
    """

    height: int
    label: str
    video_format: str
    audio_format: str | None = None
    muxed: bool = False


class SubtitleTrack(BaseModel):
    lang: str
    name: str
    auto: bool = False  # machine-generated captions


class StreamInfo(BaseModel):
    """Everything the player needs about one video."""

    video_id: str
    title: str = ""
    uploader: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    qualities: list[QualityOption] = Field(default_factory=list)  # best first
    audio_format: str | None = None  # best audio-only stream ("audio only" mode)
    formats: list[StreamFormat] = Field(default_factory=list)
    subtitles: list[SubtitleTrack] = Field(default_factory=list)
    live: bool = False


class SubtitleSource(BaseModel):
    """A subtitle track of a *downloaded* file (embedded stream or sidecar file)."""

    id: str  # "emb:<stream index>" | "side:<file name>"
    lang: str
    label: str
    source: Literal["embedded", "sidecar"]
