"""YouTube cookies endpoints: paste a Netscape cookies.txt in the UI, see whether it is loaded."""

from __future__ import annotations

from fastapi import APIRouter

from api.schemas import CookieUpload
from models import CookieStatus, CookieTestResult
from services import cookie_service, stream_service

router = APIRouter(tags=["cookies"])


@router.get("/cookies", response_model=CookieStatus)
def cookies_status() -> CookieStatus:
    """Whether cookies are loaded (counts and flags only — values are never returned)."""
    return cookie_service.status()


@router.put("/cookies", response_model=CookieStatus)
def cookies_save(body: CookieUpload) -> CookieStatus:
    """Validate and store pasted Netscape cookies; takes effect immediately (no restart)."""
    status = cookie_service.save(body.text)
    stream_service.clear()  # cached lookups were made with the old cookies
    return status


@router.delete("/cookies", response_model=CookieStatus)
def cookies_clear() -> CookieStatus:
    """Remove the stored cookies file."""
    status = cookie_service.clear()
    stream_service.clear()
    return status


@router.post("/cookies/test", response_model=CookieTestResult)
def cookies_test() -> CookieTestResult:
    """Live check: can yt-dlp read a public video with the stored cookies right now?"""
    return cookie_service.test()
