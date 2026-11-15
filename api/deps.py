"""
Request-scoped dependencies shared by the routers.

    Owner        the visitor id (server mode) that scopes the queue; None in personal mode
    admin_only   gate for management endpoints (server mode needs an admin session)
    rate(name)   per-IP request budget for expensive endpoints (server mode)

The visitor id itself is assigned by :func:`visitor_middleware` (an HttpOnly cookie),
registered by ``api.main`` only when ``SERVER_MODE=true``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from services import access_service, analytics
from services.access import ADMIN_COOKIE, VISITOR_COOKIE

_VISITOR_MAX_AGE = 400 * 86400  # browsers cap cookies at ~400 days


def client_ip(request: Request) -> str:
    """Caller address (uvicorn's ``--proxy-headers`` makes this the real client behind a proxy)."""
    return request.client.host if request.client else "unknown"


def attachment(filename: str) -> str:
    """``Content-Disposition`` for any file name (RFC 5987: non-ASCII playlist names must not break the header)."""
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "'")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


async def visitor_middleware(request: Request, call_next: Callable) -> Response:
    """
    Server mode: refuse banned addresses, give every browser an anonymous id (cookie) exposed as
    ``request.state.visitor``, and record that it was seen (the admin's visitor list).
    The /admin dashboard and its API are not counted as visitor traffic.
    """
    ip = client_ip(request)
    path = request.url.path
    admin_area = path == "/admin" or path.startswith(("/api/admin", "/static/admin"))
    if not admin_area and await run_in_threadpool(access_service.is_banned, ip):
        return JSONResponse({"detail": "Access from your address has been blocked"}, status_code=403)
    vid = request.cookies.get(VISITOR_COOKIE)
    fresh = not access_service.valid_visitor_id(vid)
    if fresh:
        vid = access_service.new_visitor_id()
    request.state.visitor = vid
    if not admin_area and path.startswith("/api/"):
        await run_in_threadpool(analytics.seen, vid, ip, request.headers.get("user-agent", ""))
    response = await call_next(request)
    if fresh:
        response.set_cookie(
            VISITOR_COOKIE, vid, max_age=_VISITOR_MAX_AGE, httponly=True, samesite="lax", secure=is_https(request), path="/"
        )
    return response


def _owner(request: Request) -> str | None:
    """The visitor whose queue this request sees (None = personal mode, no scoping)."""
    if not access_service.server_mode:
        return None
    return getattr(request.state, "visitor", None) or "anonymous"


Owner = Annotated[str | None, Depends(_owner)]


def admin_token(request: Request) -> str | None:
    return request.cookies.get(ADMIN_COOKIE)


def superadmin(request: Request) -> None:
    """The /admin dashboard API: an admin session (or personal mode without a password)."""
    access_service.require_superadmin(admin_token(request))


def admin_only(request: Request) -> None:
    """Server mode: refuse unless the caller holds an admin session (403)."""
    access_service.require_admin(admin_token(request))


def rate(bucket: str, limit: int | None = None) -> Callable[[Request], None]:
    """Dependency factory: count this call against the caller's per-minute budget (429 when spent)."""

    def check(request: Request) -> None:
        access_service.hit(bucket, client_ip(request), limit)

    return check
