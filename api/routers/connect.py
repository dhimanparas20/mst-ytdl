"""Link a YouTube account through the browser extension (one-time code, no cookie pasting)."""

from __future__ import annotations

import io
import json
import zipfile

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from api.deps import Owner, rate
from api.schemas import ConnectUpload
from configs import BASE_DIR, get_settings
from services import UnavailableError, cache_service, connect_service, stream_service

router = APIRouter(tags=["connect"])

EXTENSION_DIR = BASE_DIR / "extension"
_FILES = ("manifest.json", "popup.html", "popup.css", "popup.js", "icon.png")


@router.post("/connect", dependencies=[Depends(rate("connect", 10))])
def connect_start(who: Owner, request: Request) -> dict:
    """New one-time pairing code for the extension: ``{code, expires_in, server}``."""
    return {**connect_service.start(who), "server": str(request.base_url).rstrip("/")}


@router.get("/connect/extension.zip")
def connect_extension(request: Request) -> Response:
    """The extension as a ZIP, pre-set to THIS server (its address is baked into ``config.json`` and the manifest)."""
    if not (EXTENSION_DIR / "manifest.json").is_file():  # e.g. a container created before the folder was mounted
        raise UnavailableError("The extension files are missing on this server (extension/ is not in the app container).")
    origin = str(request.base_url).rstrip("/")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in _FILES:
            path = EXTENSION_DIR / name
            if not path.is_file():
                continue
            data = path.read_bytes()
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["host_permissions"] = [*manifest.get("host_permissions", []), origin + "/*"]
                data = json.dumps(manifest, indent=2).encode()
            z.writestr(name, data)
        z.writestr("config.json", json.dumps({"server": origin}))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="mst-ytdl-connect.zip"', "Cache-Control": "no-store"})


@router.get("/connect/{code}", dependencies=[Depends(rate("connect-poll", 150))])
def connect_status(code: str, who: Owner) -> dict:
    """``{state: waiting | done, login?, count?}`` — only for the visitor that asked for the code."""
    return connect_service.status(code, who)


@router.post("/connect/{code}", dependencies=[Depends(rate("connect", 10))])
def connect_submit(code: str, body: ConnectUpload) -> dict:
    """The extension's upload. The code is the only credential (single use, 10 minutes)."""
    out = connect_service.submit(code, body.text)
    stream_service.clear()  # cached lookups were made with the old cookies
    if not get_settings().server.mode:  # server mode: caches are per visitor (cookie_scope_tag), keep the shared ones
        cache_service.clear_json()
    return out
