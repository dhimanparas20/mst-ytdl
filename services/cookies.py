"""YouTube cookies: validate, store and report on a Netscape ``cookies.txt`` (values are never returned)."""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from yt_dlp import YoutubeDL

from configs import get_settings
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
from services.ytdl_opts import base_extract_opts, friendly_error

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
            st.count = len(cookies)
            st.expired = sum(c.expired() for c in cookies)
            login = [c for c in cookies if c.name in LOGIN_NAMES]
            st.login = bool(login)
            persistent = [c.expires for c in login if c.expires > 0]
            if persistent:
                st.expires_at = _iso(min(persistent))
            st.updated_at = _iso(path.stat().st_mtime)
            st.loaded = bool(cookies)
            st.source = "file" if cookies else "none"
            if cookies and not st.login:
                st.warnings.append("No signed-in session cookies (SID / __Secure-3PSID / LOGIN_INFO) — "
                                   "looks like a logged-out export.")
            if login and all(c.expired() for c in login):
                st.warnings.append("The login cookies have expired — export fresh ones.")
            elif persistent and min(persistent) - time.time() < _SOON_S:
                st.warnings.append("The login cookies expire within a week — plan to re-export.")
        if browser:
            st.loaded = True
            if st.source == "none":
                st.source = "browser"
        return st

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
        self._write(render_netscape(kept))
        log.info("cookies saved: %d kept, %d ignored, %d unparsable lines", len(kept), len(cookies) - len(kept), bad)
        st = self.status()
        if len(cookies) > len(kept):
            st.warnings.append(f"Ignored {len(cookies) - len(kept)} cookies from other sites.")
        if bad:
            st.warnings.append(f"Skipped {bad} unreadable line(s).")
        return st

    def test(self) -> CookieTestResult:
        """
        Ask YouTube for a public video's metadata with the current cookies.

        Catches what a format check cannot: expired or revoked sessions, bot-check blocks and a
        missing JS runtime.

        Returns:
            ``ok`` plus a human message (never raises for YouTube-side failures).
        """
        st = self.status()
        t0 = time.monotonic()

        def took() -> int:
            return int((time.monotonic() - t0) * 1000)

        try:
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


cookie_service = CookieService()
