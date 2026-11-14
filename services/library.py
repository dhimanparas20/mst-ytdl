"""
LibraryService — browse, serve and manage the downloads folder.

Every path coming from a client is resolved through ``safe_path`` which
rejects anything that escapes the downloads root (``..``, absolute paths,
symlink tricks) — the API never touches the filesystem with a raw string.
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from configs import get_ffmpeg_path, get_settings
from models import SubtitleSource
from modules.ytdlp_util import sanitize_filename, sanitize_relpath
from services.errors import ConflictError, InvalidRequestError, NotFoundError

_PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp")


_SUB_STREAM = re.compile(r"Stream #0:(\d+)(?:\[0x[0-9a-fA-F]+\])?(?:\((\w+)\))?: Subtitle: (\w+)")
_SRT_TIME = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")


_COVER_AUDIO = frozenset({".mp3", ".m4a", ".aac", ".opus", ".flac", ".ogg", ".wav"})
_COVER_SIDECAR = (".jpg", ".jpeg", ".png", ".webp")
_COVER_MAX_PX = 640
_cover_gate = threading.BoundedSemaphore(3)  # a library page asks for many covers at once; ffmpeg runs 3 at a time


def _hidden(name: str) -> bool:
    """Dot-names (``.tmp`` scratch dirs, ``.gitkeep``…) are never shown, zipped or counted."""
    return name.startswith(".")


class LibraryEntry(BaseModel):
    """One file or folder under the downloads root."""

    name: str
    path: str  # posix, relative to the downloads root ('' = root)
    is_dir: bool
    size: int = 0  # bytes (folders: total of their files)
    files: int = 0  # folders: number of files inside
    modified: float = 0.0
    ext: str | None = None


class LibraryService:
    """Singleton: ``library_service``."""

    @property
    def root(self) -> Path:
        return get_settings().download.downloads_dir

    def safe_path(self, rel: str = "") -> Path:
        """
        Resolve ``rel`` under the downloads root.

        Raises:
            InvalidRequestError: the path escapes the root.
        """
        root = self.root.resolve()
        target = (root / (rel or "")).resolve()
        if target != root and root not in target.parents:
            raise InvalidRequestError("Path is outside the downloads folder")
        return target

    def _rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root.resolve()).as_posix() if p.resolve() != self.root.resolve() else ""

    def _entry(self, p: Path) -> LibraryEntry:
        st = p.stat()
        if p.is_dir():
            files = [f for f in p.rglob("*") if self._is_media(f)]
            return LibraryEntry(
                name=p.name,
                path=self._rel(p),
                is_dir=True,
                size=sum(f.stat().st_size for f in files),
                files=len(files),
                modified=st.st_mtime,
            )
        return LibraryEntry(
            name=p.name,
            path=self._rel(p),
            is_dir=False,
            size=st.st_size,
            files=1,
            modified=st.st_mtime,
            ext=p.suffix.lstrip(".").lower() or None,
        )

    def list_dir(self, rel: str = "") -> list[LibraryEntry]:
        """Folders first, then files; newest first inside each group."""
        d = self.safe_path(rel)
        if not d.exists():
            if not rel:
                return []
            raise NotFoundError("Folder not found")
        if not d.is_dir():
            raise InvalidRequestError("Not a folder")
        entries = [
            self._entry(p)
            for p in d.iterdir()
            if not _hidden(p.name) and not p.name.endswith(_PARTIAL_SUFFIXES)
        ]
        return sorted(entries, key=lambda e: (not e.is_dir, -e.modified))

    def get_file(self, rel: str) -> Path:
        """Resolve a downloadable file path (must exist and be a file)."""
        p = self.safe_path(rel)
        if not p.is_file():
            raise NotFoundError("File not found")
        return p

    def _is_media(self, f: Path) -> bool:
        """A finished, visible file: not partial, and no hidden (dot) segment anywhere under the root."""
        if not f.is_file() or f.name.endswith(_PARTIAL_SUFFIXES):
            return False
        try:
            parts = f.resolve().relative_to(self.root.resolve()).parts
        except ValueError:
            parts = (f.name,)
        return not any(_hidden(seg) for seg in parts)

    @staticmethod
    def _stream_zip(entries: list[tuple[Path, str]]) -> Iterator[bytes]:
        """Yield a ZIP (stored, no compression — media is already compressed) chunk by chunk,
        so big playlists never need RAM proportional to the archive."""
        buf = _DrainBuffer()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for f, arcname in entries:
                with zf.open(arcname, "w", force_zip64=True) as dst, f.open("rb") as src:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
                        yield from buf.drain()
                yield from buf.drain()
        yield from buf.drain()

    # ── subtitles of downloaded videos ──────────────────────────────

    def subtitle_sources(self, rel: str) -> list[SubtitleSource]:
        """
        Subtitle tracks the browser can show for a downloaded video: streams embedded in the
        container (found with ffmpeg) plus ``<name>*.srt|vtt`` files next to it.

        Raises:
            NotFoundError / InvalidRequestError: bad path.
        """
        f = self.get_file(rel)
        found: list[SubtitleSource] = []
        ffmpeg = get_ffmpeg_path()
        if ffmpeg:
            try:
                r = subprocess.run(
                    [ffmpeg, "-hide_banner", "-i", str(f)], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=20,
                )
                for m in _SUB_STREAM.finditer(r.stderr):
                    idx, lang, codec = m.group(1), m.group(2) or "und", m.group(3)
                    found.append(SubtitleSource(id=f"emb:{idx}", lang=lang, label=f"{lang} · {codec}", source="embedded"))
            except (subprocess.SubprocessError, OSError):
                pass  # unreadable container: just no embedded tracks
        for p in sorted(f.parent.iterdir()):
            if p.is_file() and p.suffix.lower() in (".vtt", ".srt") and p.name.startswith(f.stem):
                lang = p.stem[len(f.stem):].strip(". ") or "und"
                found.append(SubtitleSource(id=f"side:{p.name}", lang=lang, label=p.name, source="sidecar"))
        return found

    def cover(self, rel: str) -> tuple[bytes, str]:
        """
        Album art of a downloaded audio file as ``(image bytes, media type)``.

        Looks for a same-named image next to the file (yt-dlp's thumbnail), then extracts the cover
        embedded in the tags with ffmpeg (downscaled JPEG, cached under ``downloads/.covers``).

        Raises:
            NotFoundError: the file has no cover (or is not audio).
        """
        f = self.get_file(rel)
        if f.suffix.lower() not in _COVER_AUDIO:
            raise NotFoundError("No cover for this file type")
        for ext in _COVER_SIDECAR:
            side = f.with_suffix(ext)
            if side.is_file() and side.stat().st_size > 0:
                return side.read_bytes(), {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                                           ".webp": "image/webp"}[ext]
        st = f.stat()
        key = hashlib.sha1(f"{rel}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:20]
        cache = self.root / ".covers"
        hit, none = cache / f"{key}.jpg", cache / f"{key}.none"
        if hit.is_file():
            return hit.read_bytes(), "image/jpeg"
        if none.is_file():
            raise NotFoundError("No embedded cover")
        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            raise InvalidRequestError("ffmpeg is not available")
        scale = f"scale='min({_COVER_MAX_PX},iw)':-2"
        with _cover_gate:
            try:
                r = subprocess.run(
                    [ffmpeg, "-v", "error", "-i", str(f), "-map", "0:v:0", "-vf", scale, "-frames:v", "1",
                     "-c:v", "mjpeg", "-q:v", "4", "-f", "image2pipe", "-"],
                    capture_output=True, timeout=30,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                raise InvalidRequestError(f"Could not read the cover: {exc}") from exc
        try:
            cache.mkdir(parents=True, exist_ok=True)
            (hit if (r.returncode == 0 and r.stdout) else none).write_bytes(r.stdout if r.returncode == 0 else b"")
        except OSError:
            pass  # caching is best-effort
        if r.returncode != 0 or not r.stdout:
            raise NotFoundError("No embedded cover")
        return r.stdout, "image/jpeg"

    def subtitle_vtt(self, rel: str, track_id: str) -> str:
        """One subtitle track of a downloaded video as WebVTT (``track_id`` from :meth:`subtitle_sources`)."""
        f = self.get_file(rel)
        kind, _, ref = track_id.partition(":")
        if kind == "side":
            if Path(ref).name != ref:  # basename only — no traversal
                raise InvalidRequestError("Bad subtitle name")
            p = f.parent / ref
            if not p.is_file() or p.suffix.lower() not in (".vtt", ".srt"):
                raise NotFoundError("Subtitle file not found")
            text = p.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
            return text if text.startswith("WEBVTT") else "WEBVTT\n\n" + _SRT_TIME.sub(r"\1.\2", text)
        if kind == "emb" and ref.isdigit():
            ffmpeg = get_ffmpeg_path()
            if not ffmpeg:
                raise InvalidRequestError("ffmpeg is not available")
            try:
                r = subprocess.run(
                    [ffmpeg, "-v", "error", "-i", str(f), "-map", f"0:{int(ref)}", "-f", "webvtt", "-"],
                    capture_output=True, timeout=60,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                raise InvalidRequestError(f"Could not read the subtitle track: {exc}") from exc
            if r.returncode != 0 or not r.stdout:
                raise NotFoundError("That subtitle track could not be extracted")
            return r.stdout.decode("utf-8", errors="replace")
        raise InvalidRequestError("Unknown subtitle track")

    def zip_folder(self, rel: str) -> tuple[str, Iterator[bytes]]:
        """
        Zip one folder; names inside the archive are relative to that folder.

        Returns:
            ``(zip_name, byte_iterator)``.

        Raises:
            NotFoundError: not a folder, or it holds no files.
        """
        folder = self.safe_path(rel)
        if not folder.is_dir():
            raise NotFoundError("Folder not found")
        entries = [(f, f.relative_to(folder).as_posix()) for f in sorted(folder.rglob("*")) if self._is_media(f)]
        if not entries:
            raise NotFoundError("Folder has no files")
        return f"{folder.name or 'downloads'}.zip", self._stream_zip(entries)

    def zip_paths(self, rels: list[str]) -> tuple[str, Iterator[bytes]]:
        """
        Zip any selection of files and folders. A lone folder behaves like
        :meth:`zip_folder`; otherwise entries keep their path relative to the
        downloads root (so two selected folders stay separate inside the archive).

        Raises:
            InvalidRequestError: empty selection or a path escaping the root.
            NotFoundError: a path does not exist, or nothing in it can be zipped.
        """
        unique = list(dict.fromkeys(r for r in rels if r is not None))
        if not unique:
            raise InvalidRequestError("Nothing selected")
        resolved = [self.safe_path(r) for r in unique]
        for p in resolved:
            if not p.exists():
                raise NotFoundError(f"Not found: {p.name}")
        if len(resolved) == 1 and resolved[0].is_dir():
            return self.zip_folder(unique[0])
        root = self.root.resolve()
        seen: dict[str, Path] = {}
        for p in resolved:
            files = [p] if p.is_file() else sorted(p.rglob("*"))
            for f in files:
                if self._is_media(f):
                    seen.setdefault(f.relative_to(root).as_posix(), f)
        if not seen:
            raise NotFoundError("Nothing to zip")
        return "selection.zip", self._stream_zip([(f, arc) for arc, f in seen.items()])

    def delete_many(self, rels: list[str]) -> dict:
        """
        Delete many files/folders. Failures are reported per path and never abort the batch.

        Returns:
            ``{"files": files removed, "deleted": paths removed, "errors": [{"path","error"}]}``
        """
        files, deleted, errors = 0, 0, []
        for rel in dict.fromkeys(rels):
            try:
                files += self.delete(rel)
                deleted += 1
            except (InvalidRequestError, NotFoundError) as exc:
                errors.append({"path": rel, "error": str(exc)})
        return {"files": files, "deleted": deleted, "errors": errors}

    def move(self, rels: list[str], dest: str) -> dict:
        """
        Move files/folders into ``dest`` (a folder under the root, created on demand;
        nested names like ``Music/Rock`` are allowed). Existing targets are never overwritten.

        Returns:
            ``{"moved": n, "dest": normalised dest, "errors": [{"path","error"}]}``
        """
        dest_rel = sanitize_relpath(dest, fallback="") if dest else ""
        dest_dir = self.safe_path(dest_rel)
        dest_dir.mkdir(parents=True, exist_ok=True)
        root = self.root.resolve()
        moved, errors = 0, []
        for rel in dict.fromkeys(rels):
            try:
                src = self.safe_path(rel)
                if src == root:
                    raise InvalidRequestError("Refusing to move the downloads root")
                if not src.exists():
                    raise NotFoundError("Not found")
                if src.is_dir() and (dest_dir == src or src in dest_dir.parents):
                    raise InvalidRequestError("Cannot move a folder into itself")
                if src.parent == dest_dir:
                    continue  # already there
                target = dest_dir / src.name
                if target.exists():
                    raise ConflictError(f"'{src.name}' already exists in the destination")
                shutil.move(str(src), str(target))
                moved += 1
            except (InvalidRequestError, NotFoundError, ConflictError) as exc:
                errors.append({"path": rel, "error": str(exc)})
        return {"moved": moved, "dest": dest_rel, "errors": errors}

    def rename(self, rel: str, new_name: str) -> LibraryEntry:
        """
        Rename a file or folder in place. A file keeps its extension when the new
        name omits one.

        Raises:
            InvalidRequestError: root, or the name is empty after sanitising.
            NotFoundError: source missing.
            ConflictError: the new name already exists.
        """
        src = self.safe_path(rel)
        if src == self.root.resolve():
            raise InvalidRequestError("Refusing to rename the downloads root")
        if not src.exists():
            raise NotFoundError("Not found")
        name = sanitize_filename(new_name, fallback="")
        if not name:
            raise InvalidRequestError("Give it a name")
        if src.is_file() and not Path(name).suffix:
            name += src.suffix
        target = src.with_name(name)
        if target != src:
            if target.exists():
                raise ConflictError(f"'{name}' already exists")
            src.rename(target)
        return self._entry(target)

    def list_folders(self, max_depth: int = 3, limit: int = 500) -> list[str]:
        """Existing folders (posix, relative to the root), shallow first — feeds the folder-name picker."""
        root = self.root.resolve()
        if not root.exists():
            return []
        out: list[str] = []
        for d in sorted(root.rglob("*")):
            if d.is_dir():
                rel = d.relative_to(root)
                if len(rel.parts) <= max_depth and not any(_hidden(seg) for seg in rel.parts):
                    out.append(rel.as_posix())
        out.sort(key=lambda p: (p.count("/"), p.lower()))
        return out[:limit]

    def delete(self, rel: str) -> int:
        """
        Delete a file or an entire folder under the root. Returns files removed.

        Raises:
            InvalidRequestError: attempting to delete the root itself.
        """
        p = self.safe_path(rel)
        if p == self.root.resolve():
            raise InvalidRequestError("Refusing to delete the downloads root")
        if not p.exists():
            raise NotFoundError("Not found")
        if p.is_file():
            p.unlink()
            return 1
        count = 0
        for f in sorted(p.rglob("*"), reverse=True):
            if f.is_file():
                f.unlink()
                count += 1
            elif f.is_dir():
                f.rmdir()
        p.rmdir()
        return count

    def usage(self) -> dict[str, int]:
        """Total files/bytes under the root (for the dashboard)."""
        root = self.root
        if not root.exists():
            return {"files": 0, "bytes": 0}
        files = [f for f in root.rglob("*") if self._is_media(f)]
        return {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}


class _DrainBuffer(io.RawIOBase):
    """Write-only, non-seekable sink that hands written bytes back to a generator."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._pos = 0

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:  # type: ignore[override]
        self._chunks.append(bytes(b))
        self._pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self._pos

    def drain(self) -> Iterator[bytes]:
        chunks, self._chunks = self._chunks, []
        yield from chunks


library_service = LibraryService()
