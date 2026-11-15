"""Browse, stream, zip, move, rename and delete what has been downloaded.

Server mode: browsing / playing / zipping is open to everyone; delete, move and rename need the admin.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, StreamingResponse

from api.deps import admin_only
from api.schemas import ActionResponse, MoveRequest, PathsRequest, RenameRequest
from services import LibraryEntry, library_service

router = APIRouter(prefix="/library", tags=["library"])


@router.get("", response_model=list[LibraryEntry])
def list_library(path: str = Query("", description="Folder relative to the downloads root")) -> list[LibraryEntry]:
    """Entries of a folder (folders first, newest first)."""
    return library_service.list_dir(path)


@router.get("/usage")
def usage() -> dict[str, int]:
    """Library files / bytes plus the total / used / free bytes of its disk (the storage bar)."""
    return library_service.usage()


@router.get("/folders", response_model=list[str])
def folders() -> list[str]:
    """Every existing folder (relative paths, ≤3 levels) — feeds the 'custom folder' picker."""
    return library_service.list_folders()


@router.get("/file")
def get_file(path: str, download: bool = False) -> FileResponse:
    """
    Serve a file (supports HTTP Range, so the browser can seek in audio/video).
    ``download=true`` forces a Save-As instead of inline playback.
    """
    f = library_service.get_file(path)
    return FileResponse(
        f,
        filename=f.name,
        content_disposition_type="attachment" if download else "inline",
    )


@router.get("/zip")
def get_zip(path: Annotated[list[str], Query(description="One or more files/folders (repeat the parameter)")]) -> StreamingResponse:
    """Stream files/folders (e.g. a playlist, or a multi-selection) as a ZIP archive."""
    name, chunks = library_service.zip_paths(path)
    return StreamingResponse(
        chunks,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.delete("", response_model=ActionResponse, dependencies=[Depends(admin_only)])
def delete_path(path: str) -> ActionResponse:
    """Delete a file or folder from disk (cannot delete the root)."""
    n = library_service.delete(path)
    return ActionResponse(affected=n, message=f"{n} file(s) deleted")


@router.post("/bulk-delete", dependencies=[Depends(admin_only)])
def bulk_delete(body: PathsRequest) -> dict:
    """Delete many files/folders; per-path failures are listed in ``errors``."""
    return library_service.delete_many(body.paths)


@router.post("/move", dependencies=[Depends(admin_only)])
def move(body: MoveRequest) -> dict:
    """Move files/folders into a destination folder (created if missing, never overwrites)."""
    return library_service.move(body.paths, body.dest)


@router.post("/rename", response_model=LibraryEntry, dependencies=[Depends(admin_only)])
def rename(body: RenameRequest) -> LibraryEntry:
    """Rename a file or folder in place."""
    return library_service.rename(body.path, body.name)
