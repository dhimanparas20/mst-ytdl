"""
FileManagerService — the admin's file manager over the whole downloads folder.

Unlike ``library_service`` (what visitors see), this one also shows and manages hidden entries
(``.deliveries``, ``.tmp``, ``.covers``). Every path still goes through :meth:`safe_path`, so
nothing outside the downloads root can be reached; the root itself can never be deleted or moved.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from configs import get_settings
from modules.ytdlp_util import sanitize_filename, sanitize_relpath
from services.errors import ConflictError, InvalidRequestError, NotFoundError
from services.library import library_service

_TEXT_EXT = {".txt", ".log", ".json", ".srt", ".vtt", ".lrc", ".nfo", ".md", ".csv"}
_TEXT_MAX = 256 * 1024


class FileEntry(BaseModel):
    """One file or folder for the admin file manager."""

    name: str
    path: str
    is_dir: bool
    size: int = 0  # folders: total of everything inside
    files: int = 0  # folders: files inside (recursive)
    modified: float = 0.0
    ext: str | None = None
    hidden: bool = False


class FileManagerService:
    """Singleton: ``file_manager``."""

    @property
    def root(self) -> Path:
        return get_settings().download.downloads_dir.resolve()

    def safe_path(self, rel: str = "") -> Path:
        """
        Resolve ``rel`` under the downloads root (hidden entries allowed).

        Raises:
            InvalidRequestError: the path escapes the root.
        """
        root = self.root
        target = (root / (rel or "").lstrip("/\\")).resolve()
        if target != root and root not in target.parents:
            raise InvalidRequestError("Path is outside the downloads folder")
        return target

    def _rel(self, p: Path) -> str:
        p = p.resolve()
        return "" if p == self.root else p.relative_to(self.root).as_posix()

    def _entry(self, p: Path) -> FileEntry:
        st = p.stat()
        if p.is_dir():
            size = files = 0
            for f in p.rglob("*"):
                try:
                    if f.is_file():
                        files += 1
                        size += f.stat().st_size
                except OSError:
                    continue
            return FileEntry(name=p.name, path=self._rel(p), is_dir=True, size=size, files=files,
                             modified=st.st_mtime, hidden=p.name.startswith("."))
        return FileEntry(name=p.name, path=self._rel(p), is_dir=False, size=st.st_size, files=1,
                         modified=st.st_mtime, ext=p.suffix.lstrip(".").lower() or None, hidden=p.name.startswith("."))

    def list(self, rel: str = "", hidden: bool = True) -> list[FileEntry]:
        """Entries of a folder: folders first, then files, by name."""
        d = self.safe_path(rel)
        if not d.is_dir():
            raise NotFoundError("Folder not found")
        out = []
        for p in d.iterdir():
            if not hidden and p.name.startswith("."):
                continue
            try:
                out.append(self._entry(p))
            except OSError:
                continue
        return sorted(out, key=lambda e: (not e.is_dir, e.name.lower()))

    def _movable(self, rel: str) -> Path:
        p = self.safe_path(rel)
        if p == self.root:
            raise InvalidRequestError("The downloads root cannot be changed")
        if not p.exists():
            raise NotFoundError(f"Not found: {rel}")
        return p

    def mkdir(self, rel: str) -> FileEntry:
        """Create a folder (nested names like ``Music/Rock`` allowed)."""
        clean = sanitize_relpath(rel, fallback="")
        if not clean:
            raise InvalidRequestError("Give the folder a name")
        p = self.safe_path(clean)
        if p.exists():
            raise ConflictError(f"'{clean}' already exists")
        p.mkdir(parents=True)
        return self._entry(p)

    def rename(self, rel: str, name: str) -> FileEntry:
        src = self._movable(rel)
        new = sanitize_filename(name, fallback="")
        if not new:
            raise InvalidRequestError("Give it a name")
        if src.is_file() and not Path(new).suffix and src.suffix:
            new += src.suffix
        target = src.with_name(new)
        if target != src:
            if target.exists():
                raise ConflictError(f"'{new}' already exists")
            src.rename(target)
        return self._entry(target)

    def _batch(self, rels: list[str], dest: str, op) -> dict:
        dest_dir = self.safe_path(sanitize_relpath(dest, fallback="") if dest else "")
        dest_dir.mkdir(parents=True, exist_ok=True)
        done, errors = 0, []
        for rel in dict.fromkeys(rels):
            try:
                src = self._movable(rel)
                if src.is_dir() and (dest_dir == src or src in dest_dir.parents):
                    raise InvalidRequestError("Cannot put a folder inside itself")
                target = dest_dir / src.name
                if target.exists():
                    raise ConflictError(f"'{src.name}' already exists there")
                op(src, target)
                done += 1
            except (InvalidRequestError, NotFoundError, ConflictError, OSError) as exc:
                errors.append({"path": rel, "error": str(exc)})
        return {"done": done, "errors": errors}

    def move(self, rels: list[str], dest: str) -> dict:
        return self._batch(rels, dest, lambda s, t: shutil.move(str(s), str(t)))

    def copy(self, rels: list[str], dest: str) -> dict:
        return self._batch(rels, dest, lambda s, t: shutil.copytree(s, t) if s.is_dir() else shutil.copy2(s, t))

    def delete(self, rels: list[str]) -> dict:
        """Delete files / folders (recursively). Per-path failures never abort the batch."""
        done, errors = 0, []
        for rel in dict.fromkeys(rels):
            try:
                p = self._movable(rel)
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                done += 1
            except (InvalidRequestError, NotFoundError, OSError) as exc:
                errors.append({"path": rel, "error": str(exc)})
        library_service._usage_cache = None
        return {"done": done, "errors": errors}

    def file(self, rel: str) -> Path:
        p = self.safe_path(rel)
        if not p.is_file():
            raise NotFoundError("File not found")
        return p

    def text(self, rel: str) -> str:
        """First 256 KB of a text-like file (logs, subtitles, lyrics, JSON)."""
        p = self.file(rel)
        if p.suffix.lower() not in _TEXT_EXT:
            raise InvalidRequestError("Not a text file")
        with p.open("rb") as fh:
            return fh.read(_TEXT_MAX).decode("utf-8", errors="replace")

    def zip(self, rels: list[str]) -> tuple[str, Iterator[bytes]]:
        """A ZIP of files / folders (hidden ones included), names relative to the root."""
        if not rels:
            raise InvalidRequestError("Nothing selected")
        entries: dict[str, Path] = {}
        for rel in rels:
            p = self.safe_path(rel)
            if not p.exists():
                raise NotFoundError(f"Not found: {rel}")
            for f in ([p] if p.is_file() else sorted(p.rglob("*"))):
                if f.is_file():
                    entries.setdefault(self._rel(f), f)
        if not entries:
            raise NotFoundError("Nothing to zip")
        name = (self.safe_path(rels[0]).name if len(rels) == 1 else "selection") or "downloads"
        return f"{name}.zip", library_service.zip_entries([(f, arc) for arc, f in entries.items()])

    def tree_usage(self) -> list[dict]:
        """Size of every top-level entry (hidden ones too) — what fills the disk."""
        return sorted(
            ({"name": e.name, "path": e.path, "size": e.size, "files": e.files, "hidden": e.hidden} for e in self.list("")),
            key=lambda x: -x["size"],
        )


file_manager = FileManagerService()
