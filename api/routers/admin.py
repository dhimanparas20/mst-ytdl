"""Server-mode admin session: unlock library management, cookies and runtime settings."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from api.deps import admin_only, admin_token, client_ip, is_https
from api.schemas import AdminConfigUpdate, AdminLogin, AdminStatus, ConfigKnob
from services import access_service, admin_config
from services.access import ADMIN_COOKIE

router = APIRouter(prefix="/admin", tags=["admin"])


def _status(token: str | None) -> AdminStatus:
    return AdminStatus(
        server_mode=access_service.server_mode,
        enabled=access_service.admin_enabled,
        admin=(not access_service.server_mode) or access_service.is_admin(token),
    )


@router.get("", response_model=AdminStatus)
def admin_status(request: Request) -> AdminStatus:
    """Is admin login available, and is this browser signed in? (personal mode: everything is open)"""
    return _status(admin_token(request))


@router.post("/login", response_model=AdminStatus)
def admin_login(body: AdminLogin, request: Request, response: Response) -> AdminStatus:
    """Check the admin password; on success an HttpOnly session cookie is set (403 wrong, 429 locked out)."""
    token = access_service.login(body.password, client_ip(request))
    response.set_cookie(
        ADMIN_COOKIE, token, max_age=access_service.session_seconds, httponly=True, samesite="strict",
        secure=is_https(request), path="/",
    )
    return _status(token)


@router.post("/logout", response_model=AdminStatus)
def admin_logout(request: Request, response: Response) -> AdminStatus:
    """End the admin session."""
    access_service.logout(admin_token(request))
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return _status(None)


# ── admin panel: live settings ──────────────────────────────────────


@router.get("/config", response_model=list[ConfigKnob], dependencies=[Depends(admin_only)])
def admin_config_list() -> list[ConfigKnob]:
    """Every setting the admin may change live, with its value, .env default and override flag."""
    return [ConfigKnob(**k) for k in admin_config.list()]


@router.put("/config", response_model=list[ConfigKnob], dependencies=[Depends(admin_only)])
def admin_config_update(body: AdminConfigUpdate) -> list[ConfigKnob]:
    """Change settings (validated all-or-nothing); ``reset`` puts keys back to their .env value."""
    return [ConfigKnob(**k) for k in admin_config.update(body.values, body.reset)]
