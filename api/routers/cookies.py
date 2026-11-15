"""YouTube cookies endpoints: paste a Netscape cookies.txt in the UI, see whether it is loaded."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import Owner, admin_only, rate
from api.schemas import CookieUpload
from models import CookieStatus, CookieTestResult
from services import InvalidRequestError, cookie_service, stream_service

router = APIRouter(tags=["cookies"])


# ── a visitor's own cookies (server mode) ───────────────────────────


def _visitor(who: str | None) -> str:
    if not who:
        raise InvalidRequestError("Your own cookies are a server-mode feature — use the server cookies (/api/cookies)")
    return who


@router.get("/my-cookies", response_model=CookieStatus)
def my_cookies_status(who: Owner) -> CookieStatus:
    """Your own cookies (server mode): counts, flags and when they are deleted — never values."""
    return cookie_service.visitor_status(_visitor(who))


@router.put("/my-cookies", response_model=CookieStatus, dependencies=[Depends(rate("cookies", 20))])
def my_cookies_save(body: CookieUpload, who: Owner) -> CookieStatus:
    """Store your own Netscape cookies for SERVER_VISITOR_COOKIE_HOURS; only your requests use them."""
    status = cookie_service.save_visitor(_visitor(who), body.text)
    stream_service.clear()
    return status


@router.delete("/my-cookies", response_model=CookieStatus)
def my_cookies_clear(who: Owner) -> CookieStatus:
    """Delete your cookies from the server now."""
    status = cookie_service.clear_visitor(_visitor(who))
    stream_service.clear()
    return status


@router.post("/my-cookies/test", response_model=CookieTestResult, dependencies=[Depends(rate("lookup"))])
def my_cookies_test(who: Owner) -> CookieTestResult:
    """Live check of YouTube with your cookies."""
    return cookie_service.test(owner=_visitor(who))


# ── the server's cookies ────────────────────────────────────────────


@router.get("/cookies", response_model=CookieStatus)
def cookies_status() -> CookieStatus:
    """Whether cookies are loaded (counts and flags only — values are never returned)."""
    return cookie_service.status()


@router.put("/cookies", response_model=CookieStatus, dependencies=[Depends(admin_only)])
def cookies_save(body: CookieUpload) -> CookieStatus:
    """Validate and store pasted Netscape cookies; takes effect immediately (no restart)."""
    status = cookie_service.save(body.text)
    stream_service.clear()  # cached lookups were made with the old cookies
    return status


@router.delete("/cookies", response_model=CookieStatus, dependencies=[Depends(admin_only)])
def cookies_clear() -> CookieStatus:
    """Remove the stored cookies file."""
    status = cookie_service.clear()
    stream_service.clear()
    return status


@router.post("/cookies/test", response_model=CookieTestResult, dependencies=[Depends(admin_only)])
def cookies_test() -> CookieTestResult:
    """Live check: can yt-dlp read a public video with the stored cookies right now?"""
    return cookie_service.test()
