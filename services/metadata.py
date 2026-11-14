"""
MetadataService — understand a URL before (and while) downloading it.

    resolve_target(url, mode)   parse + allow-list + single/playlist decision
    preview(url, mode)          MediaInfo for the UI (title, qualities, entries)
    expand_playlist(url)        flat entry list used by the resolve task

Network calls (yt-dlp) are isolated in ``_extract`` so tests can stub it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yt_dlp import YoutubeDL

from configs import get_settings
from models import EntryInfo, JobKind, MediaInfo, PlaylistMode
from modules import get_logger, today_str
from modules.ytdlp_util import (
    ParsedUrl,
    UrlError,
    normalize_url,
    parse_url,
    sanitize_filename,
    sanitize_relpath,
    validate_host,
    video_url,
)
from services.errors import InvalidRequestError
from services.ytdl_opts import base_extract_opts, friendly_error

logger = get_logger(__name__)

_UNAVAILABLE_TITLES = ("[private video]", "[deleted video]", "[unavailable video]")


@dataclass
class Target:
    """A validated URL together with how we will treat it."""

    parsed: ParsedUrl
    kind: JobKind
    url: str  # canonical URL handed to yt-dlp


@dataclass
class PlaylistInfo:
    """Flat expansion of a playlist / album / channel."""

    title: str
    uploader: str | None
    thumbnail: str | None
    entries: list[EntryInfo] = field(default_factory=list)


def classify(parsed: ParsedUrl, mode: PlaylistMode) -> JobKind:
    """
    Decide single vs playlist.

    ``AUTO`` treats ``watch?v=..&list=..`` as the single video (``PLAYLIST``
    forces the playlist; the preview surfaces ``ambiguous`` so users can).

    Raises:
        UrlError: the requested mode cannot apply to this URL.
    """
    if parsed.path_kind == "video":
        if mode is PlaylistMode.PLAYLIST:
            if not parsed.list_id:
                raise UrlError("This URL has no playlist to download")
            return JobKind.PLAYLIST
        return JobKind.SINGLE
    if mode is PlaylistMode.SINGLE:
        if parsed.video_id:
            return JobKind.SINGLE
        raise UrlError("This URL is a playlist; there is no single video in it")
    return JobKind.PLAYLIST


def _thumb(video_id: str | None) -> str | None:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg" if video_id else None


class MetadataService:
    """Singleton: ``metadata_service``."""

    # ── URL handling (pure) ─────────────────────────────────────────

    def resolve_target(self, url: str, mode: PlaylistMode = PlaylistMode.AUTO) -> Target:
        """
        Validate ``url`` against the allow-list and decide how to treat it.

        Raises:
            InvalidRequestError: unsupported / malformed URL (message is user-facing).
        """
        try:
            parsed = parse_url(url)
            validate_host(parsed, get_settings().download.allowed_hosts)
            kind = classify(parsed, mode)
        except UrlError as exc:
            raise InvalidRequestError(str(exc)) from exc
        return Target(parsed=parsed, kind=kind, url=normalize_url(parsed, kind is JobKind.PLAYLIST))

    def folder_for_single(self, override: str | None = None) -> str:
        """Single downloads go into today's date folder (or the override)."""
        if override:
            return sanitize_relpath(override)  # nested names like "Music/Rock" are allowed
        dl = get_settings().download
        return today_str(dl.timezone, dl.date_folder_format)

    def folder_for_playlist(self, title: str, override: str | None = None) -> str:
        """Playlists go into a folder named after the playlist."""
        if override:
            return sanitize_relpath(override, fallback="playlist")
        return sanitize_filename(title, fallback="playlist")  # a title's "/" is text, not nesting

    # ── network (yt-dlp) ────────────────────────────────────────────

    def _extract(self, url: str, *, flat: bool) -> dict[str, Any]:
        """One yt-dlp metadata call. Isolated so tests can monkeypatch it."""
        max_items = get_settings().download.max_playlist_items if flat else None
        with YoutubeDL(base_extract_opts(flat=flat, max_items=max_items)) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise InvalidRequestError("yt-dlp returned no information for this URL")
        return info

    def expand_playlist(self, url: str) -> PlaylistInfo:
        """
        Flat-extract a playlist/album/channel into entries.

        Private/deleted videos are kept but flagged ``available=False`` so the
        UI can show why they were skipped.
        """
        info = self._extract(url, flat=True)
        music = "music.youtube.com" in url
        entries: list[EntryInfo] = []
        for idx, e in enumerate(info.get("entries") or [], start=1):
            if not e:
                entries.append(EntryInfo(index=idx, url="", title="[unavailable]", available=False))
                continue
            vid = e.get("id")
            title = (e.get("title") or "").strip()
            ok = bool(vid) and title.lower() not in _UNAVAILABLE_TITLES
            thumbs = e.get("thumbnails") or []
            entries.append(
                EntryInfo(
                    index=idx,
                    video_id=vid,
                    url=video_url(vid, music=music) if vid else "",
                    title=title or "[untitled]",
                    uploader=e.get("channel") or e.get("uploader"),
                    duration=e.get("duration"),
                    thumbnail=(thumbs[-1].get("url") if thumbs else None) or _thumb(vid),
                    available=ok,
                )
            )
        thumbs = info.get("thumbnails") or []
        return PlaylistInfo(
            title=(info.get("title") or "Playlist").strip(),
            uploader=info.get("uploader") or info.get("channel"),
            thumbnail=(thumbs[-1].get("url") if thumbs else None)
            or (entries[0].thumbnail if entries else None),
            entries=entries,
        )

    def search(self, query: str, limit: int = 10, *, music: bool = False) -> list[EntryInfo]:
        """
        Text search (``ytsearchN:``) → video entries the UI can download directly.

        Args:
            query: free text (2–200 chars).
            limit: results wanted, clamped to 1..25.
            music: return YouTube Music watch URLs instead of youtube.com ones.

        Raises:
            InvalidRequestError: query too short/long or yt-dlp failed.
        """
        q = (query or "").strip()
        if not 2 <= len(q) <= 200:
            raise InvalidRequestError("Search text must be 2–200 characters")
        n = max(1, min(int(limit), 25))
        try:
            info = self._extract(f"ytsearch{n}:{q}", flat=True)
        except InvalidRequestError:
            raise
        except Exception as exc:
            raise InvalidRequestError(friendly_error(exc)[0]) from exc

        out: list[EntryInfo] = []
        for e in info.get("entries") or []:
            vid = (e or {}).get("id")
            if not vid or len(vid) != 11:  # skip channels / playlists / junk
                continue
            thumbs = e.get("thumbnails") or []
            out.append(
                EntryInfo(
                    index=len(out) + 1,
                    video_id=vid,
                    url=video_url(vid, music=music),
                    title=(e.get("title") or "").strip() or "[untitled]",
                    uploader=e.get("channel") or e.get("uploader"),
                    duration=e.get("duration"),
                    thumbnail=(thumbs[-1].get("url") if thumbs else None) or _thumb(vid),
                )
            )
        return out

    def preview(self, url: str, mode: PlaylistMode = PlaylistMode.AUTO) -> MediaInfo:
        """
        Everything the UI shows before the user commits to a download.

        Raises:
            InvalidRequestError: bad URL or yt-dlp could not read it.
        """
        target = self.resolve_target(url, mode)
        try:
            if target.kind is JobKind.PLAYLIST:
                pl = self.expand_playlist(target.url)
                return MediaInfo(
                    url=target.url,
                    kind=JobKind.PLAYLIST,
                    title=pl.title,
                    uploader=pl.uploader,
                    thumbnail=pl.thumbnail,
                    entry_count=len(pl.entries),
                    entries=pl.entries,
                    ambiguous=target.parsed.ambiguous,
                    folder_preview=self.folder_for_playlist(pl.title),
                )
            info = self._extract(target.url, flat=False)
        except InvalidRequestError:
            raise
        except Exception as exc:  # yt-dlp DownloadError & friends
            msg, _ = friendly_error(exc)
            raise InvalidRequestError(msg) from exc

        heights = sorted(
            {
                int(f["height"])
                for f in info.get("formats") or []
                if f.get("height") and f.get("vcodec") not in (None, "none")
            }
        )
        vid = info.get("id") or target.parsed.video_id
        return MediaInfo(
            url=target.url,
            kind=JobKind.SINGLE,
            title=(info.get("title") or "").strip(),
            uploader=info.get("uploader") or info.get("channel"),
            thumbnail=info.get("thumbnail") or _thumb(vid),
            duration=info.get("duration"),
            entry_count=1,
            available_heights=heights,
            ambiguous=target.parsed.ambiguous,
            folder_preview=self.folder_for_single(),
        )


metadata_service = MetadataService()
