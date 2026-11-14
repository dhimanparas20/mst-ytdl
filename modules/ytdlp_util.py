"""
yt-dlp adjacent helpers that need no settings and no I/O state:

    parse_url / validate_host / normalize_url / classify_url   URL understanding
    sanitize_filename                                          safe file / folder names
    parse_selection                                            "1,3,5-7" → [1, 3, 5, 6, 7]
    find_ffmpeg / find_js_runtime                              tool discovery

Project-agnostic: configs/ feeds these the user's preferences.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_ID_RE = re.compile(r"^[\w-]{11}$")
_YT_HOSTS = {"youtube.com", "music.youtube.com", "youtu.be", "youtube-nocookie.com"}
_CHANNEL_TABS = {"videos", "shorts", "streams", "playlists", "releases", "podcasts", "featured", "live"}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class UrlError(ValueError):
    """The URL is not something this app can download."""


@dataclass(frozen=True)
class ParsedUrl:
    """What we understood about a YouTube / YouTube Music URL."""

    original: str
    host: str  # normalised: no scheme, no www./m.
    is_music: bool
    path_kind: str  # video | playlist | channel | browse
    video_id: str | None
    list_id: str | None
    path: str

    @property
    def ambiguous(self) -> bool:
        """Carries both a video and a playlist (``watch?v=..&list=..``)."""
        return self.path_kind == "video" and bool(self.video_id and self.list_id)


def _bare_host(host: str) -> str:
    host = (host or "").lower().split(":")[0]
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def parse_url(url: str) -> ParsedUrl:
    """
    Understand a YouTube / YouTube Music URL.

    Raises:
        UrlError: not http(s), not a YouTube host, or no video/playlist found.
    """
    raw = (url or "").strip()
    if not raw:
        raise UrlError("Empty URL")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise UrlError("Only http(s) URLs are supported")
    host = _bare_host(parsed.netloc)
    if host not in _YT_HOSTS:
        raise UrlError(f"Unsupported host: {host or 'unknown'}")

    qs = parse_qs(parsed.query)
    path = parsed.path or "/"
    segs = [s for s in path.split("/") if s]
    video_id = (qs.get("v") or [None])[0]
    list_id = (qs.get("list") or [None])[0]

    if host == "youtu.be":
        video_id = segs[0] if segs else video_id
        kind = "video"
    elif segs and segs[0] in ("shorts", "live", "embed", "v") and len(segs) > 1:
        video_id, kind = segs[1], "video"
    elif segs and segs[0] == "playlist":
        kind = "playlist"
    elif segs and segs[0] == "browse" and len(segs) > 1:
        kind = "browse"
    elif segs and (segs[0].startswith("@") or segs[0] in ("channel", "c", "user")):
        kind = "channel"
    elif segs and segs[0] == "watch":
        kind = "video"
    else:
        kind = "video" if video_id else ("playlist" if list_id else "")

    if kind == "video" and video_id and not _ID_RE.match(video_id):
        raise UrlError("That does not look like a valid video id")
    if kind == "video" and not video_id:
        if list_id:
            kind = "playlist"
        else:
            raise UrlError("No video id found in the URL")
    if kind == "playlist" and not list_id:
        raise UrlError("No playlist id found in the URL")
    if not kind:
        raise UrlError("Could not find a video or playlist in the URL")

    return ParsedUrl(
        original=url.strip(),
        host=host,
        is_music=host == "music.youtube.com",
        path_kind=kind,
        video_id=video_id,
        list_id=list_id,
        path=path,
    )


def validate_host(parsed: ParsedUrl, allowed_hosts: Iterable[str]) -> None:
    """
    Enforce the configured host allow-list (``DL_ALLOWED_HOSTS``).

    Raises:
        UrlError: when the host (with or without www./m.) is not allowed.
    """
    allowed = {_bare_host(h) for h in allowed_hosts}
    if parsed.host not in allowed:
        raise UrlError(f"Host not allowed: {parsed.host}")


def normalize_url(parsed: ParsedUrl, playlist: bool) -> str:
    """Canonical URL for yt-dlp (drops tracking/index/time params)."""
    host = "music.youtube.com" if parsed.is_music else "www.youtube.com"
    if not playlist:
        return f"https://{host}/watch?v={parsed.video_id}"
    if parsed.video_id and parsed.list_id and parsed.list_id.startswith("RD"):
        # Mix / radio lists only load in the context of a video — the bare
        # playlist URL is "unviewable" on YouTube's side.
        return f"https://{host}/watch?v={parsed.video_id}&list={parsed.list_id}"
    if parsed.path_kind in ("video", "playlist") and parsed.list_id:
        return f"https://{host}/playlist?list={parsed.list_id}"
    if parsed.path_kind == "channel" and not parsed.is_music:
        segs = [s for s in parsed.path.split("/") if s]
        if segs and segs[-1] not in _CHANNEL_TABS:
            segs.append("videos")
        return f"https://{host}/" + "/".join(segs)
    # music browse pages and anything else: pass through untouched
    return f"https://{host}{parsed.path}"


def video_url(video_id: str, *, music: bool = False) -> str:
    """Watch URL for a bare video id."""
    return f"https://{'music' if music else 'www'}.youtube.com/watch?v={video_id}"


def sanitize_filename(name: str, max_len: int = 150, fallback: str = "untitled") -> str:
    """
    Make ``name`` safe as a file/folder name on Windows, macOS and Linux.

    Removes illegal characters, collapses whitespace, strips trailing dots and
    spaces (Windows), dodges reserved device names and truncates.
    """
    cleaned = re.sub(r"[\t\r\n\v\f]+", " ", str(name or ""))  # whitespace controls → space
    cleaned = _ILLEGAL.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    cleaned = cleaned[:max_len].rstrip(" .")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def sanitize_relpath(value: str | None, max_depth: int = 4, max_len: int = 80, fallback: str = "untitled") -> str:
    """
    Make a user-typed *relative folder path* safe: split on ``/`` or ``\\``, sanitise every
    segment, drop empty / ``.`` / ``..`` segments and cap the depth.

    Examples: ``"Music/Rock"`` → ``"Music/Rock"``; ``"../../etc"`` → ``"etc"``;
    ``"C:\\Windows"`` → ``"C Windows"``-style flattening (drive colon removed).
    """
    parts: list[str] = []
    for seg in re.split(r"[\\/]+", str(value or "")):
        seg = seg.strip()
        if seg in ("", ".", ".."):
            continue
        clean = sanitize_filename(seg, max_len=max_len, fallback="")
        if clean:
            parts.append(clean)
    return "/".join(parts[:max_depth]) or fallback


def parse_selection(spec: str | None, total: int) -> list[int]:
    """
    Parse a 1-based playlist selection into sorted unique indices.

    Examples (total=10): ``"1,3,5-7"`` → ``[1,3,5,6,7]``; ``"8-"`` → ``[8,9,10]``;
    ``None``/``""`` → all.

    Raises:
        ValueError: malformed spec, or nothing selectable.
    """
    if not spec or not spec.strip():
        return list(range(1, total + 1))
    picked: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            lo = int(lo_s) if lo_s else 1
            hi = int(hi_s) if hi_s else total
            if lo > hi:
                raise ValueError(f"Bad range: {part}")
            picked.update(range(lo, hi + 1))
        else:
            picked.add(int(part))
    result = sorted(i for i in picked if 1 <= i <= total)
    if not result:
        raise ValueError(f"Selection '{spec}' matches nothing (playlist has {total} items)")
    return result


def remove_partial_files(paths: Iterable[str]) -> int:
    """
    Delete unfinished-download leftovers for each path (the path itself and its
    ``.part`` / ``.ytdl`` / ``.temp`` companions). A directory is removed with its contents.
    Missing paths are ignored.

    Returns:
        How many files were removed.
    """
    removed = 0
    for p in paths:
        path = Path(p)
        if path.is_dir():  # a per-download temp directory: remove it with everything inside
            removed += sum(1 for f in path.rglob("*") if f.is_file())
            shutil.rmtree(path, ignore_errors=True)
            continue
        for suffix in ("", ".part", ".ytdl", ".temp"):
            f = Path(p + suffix)
            try:
                if f.is_file():
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def find_ffmpeg(configured: str | None = None) -> str | None:
    """
    Locate ffmpeg: configured path → PATH → bundled imageio-ffmpeg binary.

    Returns:
        Path to the executable, or None when nothing is available.
    """
    if configured:
        p = Path(configured)
        if p.is_file():
            return str(p)
        if p.is_dir():
            for exe in ("ffmpeg.exe", "ffmpeg"):
                if (p / exe).is_file():
                    return str(p / exe)
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe and Path(exe).is_file() else None
    except Exception:
        return None


def find_js_runtime(preference: str = "auto") -> dict | None:
    """
    Pick the JavaScript runtime yt-dlp uses to solve YouTube challenges.

    Returns:
        ``{"node": {"path": "..."}}`` style mapping for yt-dlp's ``js_runtimes``
        option, or None when none is installed.
    """
    names = ("deno", "node", "bun", "quickjs") if preference in ("", "auto") else (preference,)
    for name in names:
        exe = shutil.which(name)
        if exe:
            return {name: {"path": exe}}
    return None
