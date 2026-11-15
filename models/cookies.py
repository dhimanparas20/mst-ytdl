"""Cookie status shown in the UI (never contains cookie values)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CookieStatus(BaseModel):
    """What the server knows about its YouTube cookies — counts and flags only, no values."""

    loaded: bool = Field(description="Cookies will be sent to YouTube (file present or browser source set).")
    source: Literal["file", "browser", "none"] = "none"
    path: str = Field(description="Where the cookies file lives (or would be saved).")
    browser: str | None = Field(default=None, description="DL_COOKIES_FROM_BROWSER, when set.")
    count: int = 0
    login: bool = Field(default=False, description="Contains signed-in Google/YouTube session cookies.")
    expired: int = 0
    expires_at: str | None = Field(default=None, description="Earliest expiry (ISO, UTC) among login cookies.")
    updated_at: str | None = None
    warnings: list[str] = Field(default_factory=list)
    scope: Literal["server", "visitor"] = "server"  # whose cookies these are (visitor = server mode, yours only)
    deleted_at: str | None = Field(default=None, description="Visitor cookies: when they are deleted (ISO, UTC).")


class CookieTestResult(BaseModel):
    """Outcome of a live check of the cookies against YouTube."""

    ok: bool
    message: str
    took_ms: int = 0
