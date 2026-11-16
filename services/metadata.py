"""
MetadataService — understand a URL before (and while) downloading it.

    resolve_target(url, mode)   parse + allow-list + single/playlist decision
    preview(url, mode)          MediaInfo for the UI (title, qualities, entries)
    expand_playlist(url)        flat entry list used by the resolve task
    latest(url, limit)          uncached head of a channel / playlist (subscription polling)

Network calls (yt-dlp) are isolated in ``_extract`` so tests can stub it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from yt_dlp import YoutubeDL

from configs import get_settings
from models import EntryInfo, JobKind, MediaInfo, PlaylistMode, SearchResults
from models.paging import (
    SEARCH_DEFAULT_PAGE_SIZE,
    SEARCH_MAX_PAGE_SIZE,
    SEARCH_MAX_PAGES,
    SEARCH_POOL_CAP,
    page_window,
)
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
from services.cache import cache_service
from services.errors import InvalidRequestError
from services.runtime import runtime_service
from services.ytdl_opts import base_extract_opts, cookie_scope_tag, friendly_error

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


@dataclass
class LatestInfo:
    """The head of a channel / playlist (subscription polling)."""

    title: str
    uploader: str | None
    thumbnail: str | None
    entries: list[EntryInfo] = field(default_factory=list)
    short_ids: set[str] = field(default_factory=set)  # ids that look like YouTube Shorts
    not_ready_ids: set[str] = field(default_factory=set)  # live / premiere not started: look again later


SHORT_MAX_SECONDS = 60


def is_short_entry(raw: dict[str, Any] | None) -> bool:
    """Does a raw flat yt-dlp entry look like a YouTube Short? (``/shorts/`` URL, ``#shorts`` title, short duration)"""
    if not raw:
        return False
    if "/shorts/" in str(raw.get("url") or raw.get("webpage_url") or ""):
        return True
    if "#shorts" in str(raw.get("title") or "").lower():
        return True
    duration = raw.get("duration")
    return isinstance(duration, (int, float)) and 0 < duration <= SHORT_MAX_SECONDS


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
        """Single downloads go into the override, else the default base folder, else today's date folder."""
        if override:
            return sanitize_relpath(override)  # nested names like "Music/Rock" are allowed
        base = runtime_service.get().default_folder
        if base:
            return base
        dl = get_settings().download
        return today_str(dl.timezone, dl.date_folder_format)

    def folder_for_playlist(self, title: str, override: str | None = None) -> str:
        """Playlists go into the override, else a folder named after the playlist (inside the base folder, if set)."""
        if override:
            return sanitize_relpath(override, fallback="playlist")
        name = sanitize_filename(title, fallback="playlist")  # a title's "/" is text, not nesting
        base = runtime_service.get().default_folder
        return f"{base}/{name}" if base else name

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
        UI can show why they were skipped. Server mode caches the result
        (``SERVER_CACHE_MINUTES``): the resolve task re-reads what the preview just read.
        """
        hit = cache_service.get_json("playlist", url, cookie_scope_tag())
        if hit:
            return PlaylistInfo(
                title=hit["title"], uploader=hit["uploader"], thumbnail=hit["thumbnail"],
                entries=[EntryInfo.model_validate(e) for e in hit["entries"]],
            )
        pl = self._expand_playlist(url)
        cache_service.set_json(
            "playlist",
            {"title": pl.title, "uploader": pl.uploader, "thumbnail": pl.thumbnail,
             "entries": [e.model_dump(mode="json") for e in pl.entries]},
            url, cookie_scope_tag(),
        )
        return pl

    def feed(self, url: str, limit: int) -> list[dict[str, Any]]:
        """Flat entries of an account feed (``:ytrec``, ``/feed/playlists``, ...), at most ``limit``. Raises on yt-dlp errors."""
        with YoutubeDL(base_extract_opts(flat=True, max_items=limit)) as ydl:
            info = ydl.extract_info(url, download=False)
        return list((info or {}).get("entries") or [])

    def latest(self, url: str, limit: int) -> LatestInfo:
        """
        Fresh (never cached) flat read of the first ``limit`` entries of a channel / playlist, for
        subscription polling. Raises on yt-dlp errors — callers record them.

        Shorts are detected here, from the raw entries (``/shorts/`` URL, ``#shorts`` title,
        duration <= 60 s), because ``EntryInfo`` does not carry the raw URL.
        """
        info = self._extract_recent(url, limit)
        raw = list(info.get("entries") or [])
        entries = self.entries_from(raw, music="music.youtube.com" in url)
        thumbs = info.get("thumbnails") or []
        title = re.sub(r"\s+-\s+Videos$", "", (info.get("title") or "").strip())  # a channel's tab: "Name - Videos"
        return LatestInfo(
            title=title,
            uploader=info.get("uploader") or info.get("channel"),
            thumbnail=(thumbs[-1].get("url") if thumbs else None) or (entries[0].thumbnail if entries else None),
            entries=entries,
            short_ids={e.video_id for e, r in zip(entries, raw) if e.video_id and is_short_entry(r)},
            not_ready_ids={
                e.video_id for e, r in zip(entries, raw)
                if e.video_id and (r or {}).get("live_status") in ("is_live", "is_upcoming")
            },
        )

    def _extract_recent(self, url: str, limit: int) -> dict[str, Any]:
        """One flat yt-dlp call limited to ``limit`` entries. Isolated so tests can monkeypatch it."""
        with YoutubeDL(base_extract_opts(flat=True, max_items=limit)) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise InvalidRequestError("yt-dlp returned no information for this URL")
        return info

    @staticmethod
    def entries_from(raw: list[dict[str, Any] | None], *, music: bool = False) -> list[EntryInfo]:
        """yt-dlp flat entries → ``EntryInfo`` (private / deleted ones kept but ``available=False``)."""
        entries: list[EntryInfo] = []
        for idx, e in enumerate(raw, start=1):
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
        return entries

    def _expand_playlist(self, url: str) -> PlaylistInfo:
        info = self._extract(url, flat=True)
        entries = self.entries_from(info.get("entries") or [], music="music.youtube.com" in url)
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
            limit: results wanted, clamped to 1..``SEARCH_POOL_CAP`` (pagination pool).
            music: return YouTube Music watch URLs instead of youtube.com ones.

        Raises:
            InvalidRequestError: query too short/long or yt-dlp failed.
        """
        q = (query or "").strip()
        if not 2 <= len(q) <= 200:
            raise InvalidRequestError("Search text must be 2–200 characters")
        n = max(1, min(int(limit), SEARCH_POOL_CAP))
        hit = cache_service.get_json("search", q.lower(), n, music, cookie_scope_tag())
        if hit is not None:
            return [EntryInfo.model_validate(e) for e in hit]
        out = self._search(q, n, music)
        cache_service.set_json("search", [e.model_dump(mode="json") for e in out], q.lower(), n, music, cookie_scope_tag())
        return out

    def search_page(
        self,
        query: str,
        *,
        page: int = 1,
        page_size: int = SEARCH_DEFAULT_PAGE_SIZE,
        music: bool = False,
    ) -> SearchResults:
        """
        One page of search hits (max ``SEARCH_MAX_PAGES``).

        yt-dlp has no offset — we fetch ``page * page_size`` (+1 peek) and slice.
        """
        size = max(1, min(int(page_size), SEARCH_MAX_PAGE_SIZE))
        p = max(1, min(int(page), SEARCH_MAX_PAGES))
        need = p * size
        peek = 1 if p < SEARCH_MAX_PAGES else 0
        fetch_n = min(need + peek, SEARCH_MAX_PAGES * size)
        pool = self.search(query, fetch_n, music=music)
        if len(pool) < need:
            # Exhausted within this window — exact page count from pool length.
            page_i, pages, start = page_window(len(pool), p, size)
            entries = pool[start : start + size]
            return SearchResults(entries=entries, page=page_i, page_size=size, pages=pages)
        # Full page; peek says whether another page exists.
        has_more = len(pool) > need
        pages = min(SEARCH_MAX_PAGES, p + (1 if has_more else 0))
        # When has_more, advertise at least the next page so the pager can grow toward 10.
        if has_more and pages <= p:
            pages = min(SEARCH_MAX_PAGES, p + 1)
        start = (p - 1) * size
        return SearchResults(entries=pool[start : start + size], page=p, page_size=size, pages=max(pages, p))

    def _search(self, q: str, n: int, music: bool) -> list[EntryInfo]:
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
        hit = cache_service.get_json("preview", target.url, target.kind.value, cookie_scope_tag())
        if hit:
            return MediaInfo.model_validate(hit)
        info = self._preview(target)
        cache_service.set_json("preview", info.model_dump(mode="json"), target.url, target.kind.value, cookie_scope_tag())
        return info

    def _preview(self, target: Target) -> MediaInfo:
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
