"""The signed-in user's own YouTube: library playlists and the home feed, read through their cookies.

Personal mode uses the server's cookies (the owner's own). Server mode uses ONLY the visitor's own
cookies — never the server's, even with ``SERVER_SHARE_SERVER_COOKIES`` — so one person's account
is never shown to another. Failures never raise: they come back as a ``message`` the UI shows.
"""

from __future__ import annotations

import re

from configs import get_settings
from models import AccountFeed, AccountPlaylist, AccountPlaylists
from modules import get_logger
from services.cache import cache_service
from services.cookies import cookie_service
from services.metadata import metadata_service
from services.ytdl_opts import cookie_scope_tag, friendly_error

log = get_logger(__name__)

PLAYLISTS_URL = "https://www.youtube.com/feed/playlists"
HOME_URL = ":ytrec"  # yt-dlp's alias for the signed-in home feed
HOME_LIMIT = 40
_VIDEO_ID = re.compile(r"^[\w-]{11}$")

# Present on every account; they open as ordinary playlists (music.youtube.com keeps YouTube Music links).
PINNED: tuple[AccountPlaylist, ...] = (
    AccountPlaylist(id="LM", title="Liked music", url="https://music.youtube.com/playlist?list=LM", music=True, pinned=True),
    AccountPlaylist(id="LL", title="Liked videos", url="https://www.youtube.com/playlist?list=LL", pinned=True),
    AccountPlaylist(id="WL", title="Watch later", url="https://www.youtube.com/playlist?list=WL", pinned=True),
)
_NOT_SIGNED_IN = "Add your YouTube cookies to see your playlists and home feed."


class AccountService:
    """Reads the account's playlists / home feed (yt-dlp, cached per visitor in server mode)."""

    def signed_in(self, owner: str | None) -> bool:
        """True when the cookies that would be used carry a login session."""
        if get_settings().server.mode:
            return bool(owner) and cookie_service.visitor_status(owner).login
        st = cookie_service.status()
        return st.login or bool(st.browser)

    def playlists(self, owner: str | None) -> AccountPlaylists:
        """Pinned playlists first, then the account's library playlists."""
        if not self.signed_in(owner):
            return AccountPlaylists(signed_in=False, message=_NOT_SIGNED_IN)
        with cookie_service.scope(owner):
            tag = cookie_scope_tag()
            hit = cache_service.get_json("acc-playlists", tag)
            if hit:
                return AccountPlaylists.model_validate(hit)
            try:
                raw = metadata_service.feed(PLAYLISTS_URL, get_settings().download.max_playlist_items)
                message = None if raw else "YouTube returned no playlists — the cookies may have expired."
            except Exception as exc:  # yt-dlp raises many types
                raw, (message, _) = [], friendly_error(exc)
                log.info("account playlists failed: %s", type(exc).__name__)
        out = AccountPlaylists(signed_in=True, playlists=[*PINNED, *self._parse_playlists(raw)], message=message)
        if message is None:
            cache_service.set_json("acc-playlists", out.model_dump(mode="json"), tag)
        return out

    @staticmethod
    def _parse_playlists(raw: list[dict | None]) -> list[AccountPlaylist]:
        pinned = {p.id for p in PINNED}
        seen: set[str] = set()
        out: list[AccountPlaylist] = []
        for e in raw:
            pid = (e or {}).get("id")
            title = ((e or {}).get("title") or "").strip()
            if not pid or not title or pid in pinned or pid in seen:
                continue
            seen.add(pid)
            thumbs = e.get("thumbnails") or []
            count = e.get("playlist_count") or e.get("n_entries")
            out.append(AccountPlaylist(
                id=pid, title=title, url=f"https://www.youtube.com/playlist?list={pid}",
                thumbnail=(thumbs[-1].get("url") if thumbs else None),
                count=int(count) if count else None, uploader=e.get("channel") or e.get("uploader"),
            ))
        return out

    def home(self, owner: str | None, limit: int = HOME_LIMIT) -> AccountFeed:
        """The signed-in home feed as playable tracks (videos only; mixes and channels are skipped)."""
        if not self.signed_in(owner):
            return AccountFeed(signed_in=False, message=_NOT_SIGNED_IN)
        with cookie_service.scope(owner):
            tag = cookie_scope_tag()
            hit = cache_service.get_json("acc-home", tag, limit)
            if hit:
                return AccountFeed.model_validate(hit)
            try:
                raw = metadata_service.feed(HOME_URL, limit)
            except Exception as exc:
                msg, _ = friendly_error(exc)
                log.info("account home failed: %s", type(exc).__name__)
                return AccountFeed(signed_in=True, message=msg)
        videos = [e for e in raw if e and _VIDEO_ID.match(str(e.get("id") or "")) and (e.get("ie_key") in (None, "Youtube"))]
        entries = [e for e in metadata_service.entries_from(videos) if e.available][:limit]
        out = AccountFeed(signed_in=True, entries=entries,
                          message=None if entries else "YouTube returned an empty feed — the cookies may have expired.")
        if entries:
            cache_service.set_json("acc-home", out.model_dump(mode="json"), tag, limit)
        return out


account_service = AccountService()
