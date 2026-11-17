"""
ExploreService — Home page shelf.

    for_you           signed-in YouTube home (``:ytrec``) via account_service
    trending_music    multi-query search (works signed-in; anonymous is experimental)
    trending_videos   multi-query search (works signed-in; anonymous is experimental)

Anonymous Music / Videos shelves are search-based (YouTube's trending tab is unreliable
in yt-dlp). They can fail without cookies — the UI opts into showing them via a browser
pref (``exploreAnon``). ``for_you`` always needs a login session.

Results are paginated (``page`` / ``page_size``, max 10 pages) from a cached pool.
"""

from __future__ import annotations

from models import EntryInfo, ExploreFeed
from models.paging import SEARCH_DEFAULT_PAGE_SIZE, SEARCH_MAX_PAGES, page_window
from modules import get_logger
from services.account import account_service
from services.analytics import analytics
from services.cache import cache_service
from services.cookies import cookie_service
from services.metadata import metadata_service
from services.ytdl_opts import cookie_scope_tag

log = get_logger(__name__)

_NEED_COOKIES = "Add YouTube cookies to see For you — or browse Music / Videos."

# Several queries → a varied shelf; results are de-duped by video id.
_MUSIC_QUERIES = (
    "new music this week",
    "viral songs",
    "top hits official audio",
    "official music video",
)
_VIDEO_QUERIES = (
    "trending",
    "viral video",
    "popular today",
    "must watch",
)


class ExploreService:
    """Singleton: ``explore_service``."""

    def get(
        self,
        owner: str | None,
        kind: str = "auto",
        limit: int = SEARCH_DEFAULT_PAGE_SIZE,
        *,
        page: int = 1,
    ) -> ExploreFeed:
        """
        Home shelf for the visitor.

        Args:
            owner: visitor id in server mode (None = personal / unscoped).
            kind: ``auto`` (for_you if signed in, else music), ``music``, ``videos``, ``for_you``.
            limit: page size (1–40).
            page: 1-based page index (clamped to 1..10).

        Returns:
            ExploreFeed — never raises. ``signed_in`` reflects cookie login; anonymous
            Music/Videos still return entries when the search succeeds.
        """
        page_size = max(1, min(int(limit), 40))
        page_i = max(1, min(int(page), SEARCH_MAX_PAGES))
        k = (kind or "auto").strip().lower()
        if k not in ("auto", "music", "videos", "for_you"):
            k = "auto"

        signed = account_service.signed_in(owner)
        if k == "auto":
            k = "for_you" if signed else "music"

        with cookie_service.scope(owner):
            if k == "for_you":
                return self._for_you(owner, page_size, page_i, signed=signed)
            if k == "videos":
                return self._trending(
                    "trending_videos", "Trending videos", _VIDEO_QUERIES,
                    music=False, page_size=page_size, page=page_i, signed=signed,
                )
            return self._trending(
                "trending_music", "Trending music", _MUSIC_QUERIES,
                music=True, page_size=page_size, page=page_i, signed=signed,
            )

    def _slice(self, pool: list[EntryInfo], page: int, page_size: int) -> tuple[list[EntryInfo], int, int]:
        page_i, pages, start = page_window(len(pool), page, page_size)
        return pool[start : start + page_size], page_i, pages

    def _for_you(self, owner: str | None, page_size: int, page: int, *, signed: bool) -> ExploreFeed:
        if not signed:
            return ExploreFeed(
                source="for_you",
                title="For you",
                signed_in=False,
                message=_NEED_COOKIES,
                page=1,
                page_size=page_size,
                pages=1,
            )
        pool_n = page_size * SEARCH_MAX_PAGES
        home = account_service.home(owner, pool_n)
        entries, page_i, pages = self._slice(home.entries, page, page_size)
        return ExploreFeed(
            source="for_you",
            title="For you",
            signed_in=True,
            entries=entries,
            message=home.message,
            page=page_i,
            page_size=page_size,
            pages=pages,
        )

    def _trending(
        self,
        source: str,
        title: str,
        queries: tuple[str, ...],
        *,
        music: bool,
        page_size: int,
        page: int,
        signed: bool,
    ) -> ExploreFeed:
        pool_n = page_size * SEARCH_MAX_PAGES
        # Cache the full pool so flipping pages is free.
        hit = cache_service.get_json("explore", source, pool_n, cookie_scope_tag())
        if hit is not None:
            feed = ExploreFeed.model_validate(hit)
            if feed.signed_in != signed:
                feed = feed.model_copy(update={"signed_in": signed})
            entries, page_i, pages = self._slice(feed.entries, page, page_size)
            return feed.model_copy(update={
                "entries": entries, "page": page_i, "page_size": page_size, "pages": pages,
            })

        per = max(4, (pool_n + len(queries) - 1) // len(queries))
        seen: set[str] = set()
        out: list[EntryInfo] = []
        errors: list[str] = []
        for q in queries:
            if len(out) >= pool_n:
                break
            try:
                batch = metadata_service.search(q, min(per, pool_n - len(out) + 4), music=music)
            except Exception as exc:
                log.debug("explore query failed %r: %s", q, type(exc).__name__)
                errors.append(str(exc))
                continue
            for e in batch:
                vid = e.video_id or ""
                if not vid or vid in seen or not e.available:
                    continue
                seen.add(vid)
                out.append(e)
                if len(out) >= pool_n:
                    break

        empty_msg = (
            errors[0] if errors
            else "Nothing to show right now — try Search above, or add cookies if YouTube is blocking."
        )
        pool = out[:pool_n]
        # Cache the full pool (page fields are placeholders; sliced per request).
        cached = ExploreFeed(
            source=source,  # type: ignore[arg-type]
            title=title,
            signed_in=signed,
            entries=pool,
            message=None if pool else empty_msg,
            page=1,
            page_size=page_size,
            pages=1,
        )
        if pool:
            cache_service.set_json(
                "explore", cached.model_dump(mode="json"), source, pool_n, cookie_scope_tag(),
            )
            analytics.count("explore")

        entries, page_i, pages = self._slice(pool, page, page_size)
        return cached.model_copy(update={
            "entries": entries,
            "page": page_i,
            "page_size": page_size,
            "pages": pages if pool else 1,
            "message": None if entries else empty_msg,
        })


explore_service = ExploreService()
