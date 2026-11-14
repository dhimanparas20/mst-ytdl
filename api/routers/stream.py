"""In-app playback: YouTube streams proxied through our origin, plus subtitles."""

from __future__ import annotations

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import StreamingResponse

from models import StreamInfo, SubtitleSource
from services import library_service, stream_service

router = APIRouter(tags=["stream"])


@router.get("/stream/{video_id}/info", response_model=StreamInfo)
def stream_info(video_id: str) -> StreamInfo:
    """
    Qualities, audio-only stream and subtitle tracks for ``video_id``.
    Slow on a cold cache (~5–8 s: yt-dlp solves YouTube's JS challenge); cached ~20 min.
    """
    return stream_service.info(video_id)


@router.get("/stream/{video_id}/media")
def stream_media(
    video_id: str,
    format: str = Query(..., min_length=1, max_length=40, description="format_id from /info"),
    range: str | None = Header(default=None),
) -> StreamingResponse:
    """One stream of the video, Range-capable (seekable in <video>/<audio>)."""
    media = stream_service.open_media(video_id, format, range)
    return StreamingResponse(media.body, status_code=media.status, headers=media.headers)


@router.get("/stream/{video_id}/subs")
def stream_subtitles(video_id: str, lang: str = Query(..., max_length=20), auto: bool = False) -> Response:
    """A subtitle track as WebVTT."""
    return Response(stream_service.subtitle_vtt(video_id, lang, auto), media_type="text/vtt; charset=utf-8",
                    headers={"Cache-Control": "private, max-age=600"})


@router.get("/library/subtitles", response_model=list[SubtitleSource])
def library_subtitles(path: str) -> list[SubtitleSource]:
    """Subtitle tracks of a downloaded video (embedded streams + sidecar files)."""
    return library_service.subtitle_sources(path)


@router.get("/library/subtitle")
def library_subtitle(path: str, track: str = Query(..., max_length=300)) -> Response:
    """One subtitle track of a downloaded video as WebVTT."""
    return Response(library_service.subtitle_vtt(path, track), media_type="text/vtt; charset=utf-8")
