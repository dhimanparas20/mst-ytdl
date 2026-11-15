"""
YouTube cookies: validate, store and report on a Netscape ``cookies.txt`` (values are never returned).

Two owners of cookies:
    server cookies    one file (``DL_COOKIES_FILE`` / ``cookies/cookies.txt``) — personal mode, or the
                      admin's in server mode (used for visitors only with ``SERVER_SHARE_SERVER_COOKIES``)
    visitor cookies   server mode: each browser may paste its own; kept in Redis ``P:VCOOKIES:<visitor>``
                      for ``SERVER_VISITOR_COOKIE_HOURS`` and only ever used for that visitor's requests.

:meth:`CookieService.scope` decides which cookies a block of yt-dlp calls uses (see ``ytdl_opts``).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from yt_dlp import YoutubeDL

from configs import get_key_prefix, get_redis, get_settings
from models import CookieStatus, CookieTestResult
from modules import get_logger
from modules.cookies_util import (
    LOGIN_NAMES,
    Cookie,
    domain_matches,
    parse_netscape,
    render_netscape,
)
from services.errors import InvalidRequestError
from services.ytdl_opts import base_extract_opts, friendly_error, reset_cookie_scope, set_cookie_scope

log = get_logger(__name__)

#: Only these sites' cookies are stored — anything else pasted by accident is dropped.
_KEEP_ROOTS = ("youtube.com", "google.com", "youtube-nocookie.com")
MAX_BYTES = 512 * 1024
_SOON_S = 7 * 86_400
#: A short, always-public video used to check that YouTube accepts our requests (with the cookies).
TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")


class CookieService:
    """Owns the cookies file that yt-dlp reads (``DownloadSettings.cookies_path``)."""

    @staticmethod
    def path() -> Path:
        """Absolute path of the cookies file."""
        return get_settings().download.cookies_path

    def status(self) -> CookieStatus:
        """
        Describe the cookies currently in effect.

        Returns:
            Counts and flags only — never names or values of individual cookies.
        """
        dl = get_settings().download
        path = self.path()
        browser = dl.cookies_from_browser
        st = CookieStatus(loaded=False, path=str(path), browser=browser)
        if path.is_file():
            try:
                cookies, _ = parse_netscape(path.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                st.warnings.append(f"Cannot read the cookies file: {exc}")
                cookies = []
            self._describe(st, cookies, path.stat().st_mtime)
        if browser:
            st.loaded = True
            if st.source == "none":
                st.source = "browser"
        return st

    @staticmethod
    def _describe(st: CookieStatus, cookies: list[Cookie], updated: float) -> None:
        """Fill counts / flags / warnings of ``st`` from parsed cookies (never their values)."""
        st.count = len(cookies)
        st.expired = sum(c.expired() for c in cookies)
        login = [c for c in cookies if c.name in LOGIN_NAMES]
        st.login = bool(login)
        persistent = [c.expires for c in login if c.expires > 0]
        if persistent:
            st.expires_at = _iso(min(persistent))
        st.updated_at = _iso(updated)
        st.loaded = bool(cookies)
        st.source = "file" if cookies else "none"
        if cookies and not st.login:
            st.warnings.append("No signed-in session cookies (SID / __Secure-3PSID / LOGIN_INFO) — "
                               "looks like a logged-out export.")
        if login and all(c.expired() for c in login):
            st.warnings.append("The login cookies have expired — export fresh ones.")
        elif persistent and min(persistent) - time.time() < _SOON_S:
            st.warnings.append("The login cookies expire within a week — plan to re-export.")

    def save(self, text: str) -> CookieStatus:
        """
        Validate pasted Netscape cookies and store them (atomically, owner-only permissions).

        Args:
            text: Content of a ``cookies.txt`` export.

        Returns:
            The new status.

        Raises:
            InvalidRequestError: Empty, too large, wrong format, or no YouTube/Google cookies.
        """
        kept, ignored, bad = self._validate(text)
        self._write(render_netscape(kept))
        log.info("cookies saved: %d kept, %d ignored, %d unparsable lines", len(kept), ignored, bad)
        st = self.status()
        self._notes(st, ignored, bad)
        return st

    @staticmethod
    def _notes(st: CookieStatus, ignored: int, bad: int) -> None:
        if ignored:
            st.warnings.append(f"Ignored {ignored} cookies from other sites.")
        if bad:
            st.warnings.append(f"Skipped {bad} unreadable line(s).")

    @staticmethod
    def _validate(text: str) -> tuple[list[Cookie], int, int]:
        """
        Parse pasted Netscape text and keep only YouTube / Google cookies.

        Returns:
            ``(kept, ignored_other_sites, unparsable_lines)``.

        Raises:
            InvalidRequestError: Empty, too large, wrong format, or no YouTube/Google cookies.
        """
        text = (text or "").strip()
        if not text:
            raise InvalidRequestError("Paste the contents of your cookies.txt first.")
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise InvalidRequestError("That is too large for a cookies file (limit 512 KB).")
        if text[0] in "[{":
            raise InvalidRequestError(
                "That looks like a JSON export. Export in Netscape format (a cookies.txt file) instead."
            )
        cookies, bad = parse_netscape(text)
        if not cookies:
            raise InvalidRequestError(
                "No cookies found. Expected Netscape format: 7 tab-separated columns "
                "(domain, subdomains, path, secure, expiry, name, value)."
            )
        kept: list[Cookie] = [c for c in cookies if domain_matches(c.domain, _KEEP_ROOTS)]
        if not kept:
            raise InvalidRequestError("None of these cookies belong to youtube.com or google.com.")
        return kept, len(cookies) - len(kept), bad

    def test(self, owner: str | None = None) -> CookieTestResult:
        """
        Ask YouTube for a public video's metadata with the current cookies.

        Catches what a format check cannot: expired or revoked sessions, bot-check blocks and a
        missing JS runtime.

        Returns:
            ``ok`` plus a human message (never raises for YouTube-side failures).
        """
        st = self.visitor_status(owner) if owner else self.status()
        t0 = time.monotonic()

        def took() -> int:
            return int((time.monotonic() - t0) * 1000)

        try:
            with self.scope(owner) if owner else contextlib.nullcontext():
                with YoutubeDL({**base_extract_opts(), "quiet": True, "no_warnings": True}) as ydl:
                    info = ydl.extract_info(TEST_VIDEO, download=False)
        except Exception as exc:  # yt-dlp raises many types
            msg, _ = friendly_error(exc)
            return CookieTestResult(ok=False, message=msg, took_ms=took())
        if not (info or {}).get("formats"):
            return CookieTestResult(ok=False, message="YouTube answered but offered no playable formats.", took_ms=took())
        note = ""
        if not st.loaded:
            note = " (no cookies are loaded, so this only shows anonymous access works)"
        elif st.source == "file" and not st.login:
            note = " — but these cookies are not a signed-in session"
        return CookieTestResult(ok=True, message=f"YouTube accepted the request{note}.", took_ms=took())

    def clear(self) -> CookieStatus:
        """Remove the stored cookies file (a no-op when there is none)."""
        with contextlib.suppress(FileNotFoundError):
            self.path().unlink()
        log.info("cookies cleared")
        return self.status()

    def _write(self, content: str) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cookies-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise InvalidRequestError(f"Cannot write {path}: {exc}") from exc


    # ── visitor cookies (server mode) ───────────────────────────────

    def _vkey(self, owner: str) -> str:
        return f"{get_key_prefix()}:VCOOKIES:{owner}"

    @staticmethod
    def _visitor_ttl() -> int:
        return get_settings().server.visitor_cookie_hours * 3600

    @staticmethod
    def _scratch_dir() -> Path:
        """Where per-call temp cookie files live (private, next to the server cookies)."""
        return get_settings().download.cookies_path.parent / "visitors"

    def visitor_text(self, owner: str | None) -> str | None:
        if not owner or not get_settings().server.mode:
            return None
        return get_redis().get(self._vkey(owner))

    def visitor_status(self, owner: str) -> CookieStatus:
        """This visitor's own cookies: counts / flags, when they are deleted (never values)."""
        st = CookieStatus(loaded=False, path="(your browser only)", scope="visitor")
        r = get_redis()
        text = r.get(self._vkey(owner))
        if text:
            cookies, _ = parse_netscape(text)
            ttl = r.ttl(self._vkey(owner))
            self._describe(st, cookies, time.time())
            st.updated_at = None
            st.deleted_at = _iso(time.time() + ttl) if ttl and ttl > 0 else None
        return st

    def save_visitor(self, owner: str, text: str) -> CookieStatus:
        """
        Store a visitor's own cookies (validated like the server's) for ``SERVER_VISITOR_COOKIE_HOURS``.

        Raises:
            InvalidRequestError: bad cookies text.
        """
        kept, ignored, bad = self._validate(text)
        get_redis().set(self._vkey(owner), render_netscape(kept), ex=self._visitor_ttl())
        log.info("visitor cookies saved (%d kept)", len(kept))  # never the visitor id or values
        st = self.visitor_status(owner)
        self._notes(st, ignored, bad)
        return st

    def clear_visitor(self, owner: str) -> CookieStatus:
        get_redis().delete(self._vkey(owner))
        return self.visitor_status(owner)

    def cache_tag(self, owner: str | None) -> str:
        """Namespace for caches filled with this visitor's cookies ('' = anonymous, shared)."""
        if not owner or not self.visitor_text(owner):
            return ""
        return "v:" + hashlib.sha1(owner.encode()).hexdigest()[:16]

    @contextlib.contextmanager
    def scope(self, owner: str | None) -> Iterator[None]:
        """
        Run yt-dlp calls with the right cookies.

        Personal mode: no change (the server cookies, exactly as before). Server mode: the visitor's
        own cookies through a private temp file (yt-dlp's refreshed cookies are written back to Redis,
        the file is deleted), else no cookies at all — unless ``SERVER_SHARE_SERVER_COOKIES`` lets
        visitors use the server's.
        """
        s = get_settings().server
        if not s.mode:
            yield
            return
        text = self.visitor_text(owner)
        if not text:
            token = set_cookie_scope(None if s.share_server_cookies else {})
            try:
                yield
            finally:
                reset_cookie_scope(token)
            return
        folder = self._scratch_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(folder, 0o700)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix="v-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        token = set_cookie_scope({"cookiefile": tmp}, self.cache_tag(owner))
        try:
            yield
        finally:
            reset_cookie_scope(token)
            try:
                fresh = Path(tmp).read_text(encoding="utf-8", errors="replace")
                kept = [c for c in parse_netscape(fresh)[0] if domain_matches(c.domain, _KEEP_ROOTS)]
                if kept and fresh != text:  # YouTube rotated some cookies: keep them (same expiry)
                    get_redis().set(self._vkey(owner), render_netscape(kept), xx=True, keepttl=True)
            except OSError:
                pass
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def sweep_scratch(self, max_age_s: float = 3600) -> int:
        """Remove temp cookie files a crashed call left behind."""
        removed = 0
        folder = self._scratch_dir()
        if folder.is_dir():
            for f in folder.glob("v-*.txt"):
                with contextlib.suppress(OSError):
                    if time.time() - f.stat().st_mtime > max_age_s:
                        f.unlink()
                        removed += 1
        return removed


cookie_service = CookieService()
