"""
Translate our domain options into yt-dlp parameters.

Pure functions (no I/O except reading settings/clients) so every mapping is
unit-testable without touching the network.

    format_selector / format_sort   quality chooser → yt-dlp format logic
    build_postprocessors            mp3/m4a/... extraction, tags, cover art, subs
    build_ydl_opts                  the full ``YoutubeDL`` params dict
    friendly_error                  readable message + "worth retrying?" flag
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from configs import get_ffmpeg_path, get_js_runtimes, get_settings
from models import DownloadOptions, MediaFormat
from modules import get_logger

logger = get_logger(__name__)

# Containers yt-dlp's EmbedThumbnail can write cover art into. Embedding into
# anything else (wav, webm) would raise, so we skip it instead of failing.
_THUMBNAIL_CAPABLE = {
    MediaFormat.MP3,
    MediaFormat.M4A,
    MediaFormat.OPUS,
    MediaFormat.FLAC,
    MediaFormat.MP4,
    MediaFormat.MKV,
}
# Containers that can carry embedded (soft) subtitles.
_SUBTITLE_CAPABLE = {MediaFormat.MP4, MediaFormat.MKV, MediaFormat.WEBM}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_NON_RETRYABLE_HINTS = (
    "private video",
    "video unavailable",
    "this video is not available",
    "members-only",
    "sign in to confirm",
    "age-restricted",
    "copyright",
    "has been removed",
    "ffmpeg is not installed",
    "ffprobe and ffmpeg not found",
    "unsupported url",
    "live event will begin",
    "premieres in",
    "requested format is not available",
    "not made this video available in your country",
)


class YtdlLogger:
    """Route yt-dlp's logging into ours (debug is noisy → stays at DEBUG)."""

    def debug(self, msg: str) -> None:
        logger.debug(_ANSI.sub("", msg))

    info = debug

    def warning(self, msg: str) -> None:
        logger.warning(_ANSI.sub("", msg))

    def error(self, msg: str) -> None:
        logger.error(_ANSI.sub("", msg))


def format_selector(options: DownloadOptions) -> str:
    """
    yt-dlp ``format`` string.

    Audio: best audio stream (m4a preferred when the target is m4a to allow a
    lossless remux). Video: separate best video + best audio merged by ffmpeg,
    falling back to a pre-muxed stream. Height caps are applied through
    :func:`format_sort` so "closest to the chosen quality" semantics hold.
    """
    if options.format.is_audio:
        if options.format is MediaFormat.M4A:
            return "bestaudio[ext=m4a]/bestaudio/best"
        return "bestaudio/best"
    if options.format is MediaFormat.WEBM:
        return "bv*[ext=webm]+ba[ext=webm]/bv*+ba/b"
    return "bv*+ba/b"


def format_sort(options: DownloadOptions) -> list[str] | None:
    """
    yt-dlp ``format_sort`` keys implementing the quality chooser.

    ``res:H`` prefers the largest resolution not above H. For mp4 we then
    prefer H.264 + AAC (plays everywhere) — only possible up to 1080p, so for
    1440p/2160p the best VP9/AV1 stream wins and is muxed into the container.
    """
    if options.format.is_audio:
        return None
    height = None if options.quality == "best" else int(options.quality)
    keys: list[str] = []
    if height:
        keys.append(f"res:{height}")
    if options.format is MediaFormat.MP4:
        if height is None or height <= 1080:
            keys += ["vcodec:h264", "acodec:m4a"]
        else:
            keys += ["ext:mp4:m4a"]
    return keys or None


def build_postprocessors(options: DownloadOptions) -> list[dict[str, Any]]:
    """Post-processor chain, ordered the way yt-dlp's own CLI orders it."""
    fmt = options.format
    pps: list[dict[str, Any]] = []

    if fmt.is_audio:
        pp: dict[str, Any] = {"key": "FFmpegExtractAudio", "preferredcodec": fmt.value}
        if fmt is MediaFormat.MP3:
            pp["preferredquality"] = "320" if options.quality == "best" else options.quality
        elif fmt is MediaFormat.M4A and options.quality != "best":
            pp["preferredquality"] = options.quality
        pps.append(pp)

    if options.subtitles and fmt in _SUBTITLE_CAPABLE:
        pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"})
        pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})

    if options.embed_metadata:
        pps.append({"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True})

    if options.embed_thumbnail and fmt in _THUMBNAIL_CAPABLE:
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    return pps


def build_ydl_opts(
    options: DownloadOptions,
    *,
    outdir: Path,
    temp_dir: Path | None = None,
    filename_prefix: str = "",
    runtime_speed_limit: int | None = None,
    progress_hook: Callable[[dict], None] | None = None,
    postprocessor_hook: Callable[[dict], None] | None = None,
    post_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Complete ``YoutubeDL`` params for one item.

    Args:
        options: the job's resolved options.
        outdir: absolute destination folder (only FINISHED files ever land here).
        temp_dir: private per-download scratch folder for ``.part`` files, intermediates,
            thumbnails and subtitles — keeps parallel downloads of the same title from
            colliding and makes cancel a single directory delete.
        filename_prefix: e.g. ``"03 - "`` for numbered playlist files.
        runtime_speed_limit: UI-set cap (bytes/sec) used when the job has none.
        progress_hook / postprocessor_hook / post_hook: worker callbacks.
    """
    dl = get_settings().download
    suffix = " [%(id)s]" if dl.include_id_in_name else ""
    speed = options.speed_limit or runtime_speed_limit or dl.speed_limit

    opts: dict[str, Any] = {
        "outtmpl": {"default": f"{filename_prefix}%(title).150B{suffix}.%(ext)s"},
        "paths": {"home": str(outdir), **({"temp": str(temp_dir)} if temp_dir else {})},
        "format": format_selector(options),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": YtdlLogger(),
        "color": {"stdout": "never", "stderr": "never"},
        # resilience
        "retries": dl.network_retries,
        "fragment_retries": dl.network_retries,
        "extractor_retries": 3,
        "file_access_retries": 5,
        "socket_timeout": 30,
        "continuedl": True,  # resume .part files (powers pause/resume)
        "concurrent_fragment_downloads": max(1, dl.concurrent_fragments),
        # files
        "windowsfilenames": True,  # names valid on every OS
        "updatetime": False,  # mtime = download time (library sorts by it)
        "overwrites": True if options.overwrite else None,  # default: never clobber a finished file
        "writethumbnail": bool(options.embed_thumbnail and options.format in _THUMBNAIL_CAPABLE),
        "postprocessors": build_postprocessors(options),
    }

    sort = format_sort(options)
    if sort:
        opts["format_sort"] = sort
    if options.format.is_video:
        opts["merge_output_format"] = options.format.value
    if options.subtitles and options.format in _SUBTITLE_CAPABLE:
        opts.update(
            writesubtitles=True,
            writeautomaticsub=True,
            subtitleslangs=options.subtitle_langs,
            subtitlesformat="srt/vtt/best",
        )
    if speed:
        opts["ratelimit"] = speed

    ffmpeg = get_ffmpeg_path()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    js = get_js_runtimes()
    if js:
        opts["js_runtimes"] = js
    if dl.cookies_path.is_file():  # checked per call: saving cookies in the UI needs no restart
        opts["cookiefile"] = str(dl.cookies_path)
    if dl.cookies_from_browser:
        opts["cookiesfrombrowser"] = (dl.cookies_from_browser,)
    if dl.proxy:
        opts["proxy"] = dl.proxy

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if postprocessor_hook:
        opts["postprocessor_hooks"] = [postprocessor_hook]
    if post_hook:
        opts["post_hooks"] = [post_hook]
    return opts


def base_extract_opts(*, flat: bool = False, max_items: int | None = None) -> dict[str, Any]:
    """Options for metadata-only calls (preview / playlist expansion)."""
    dl = get_settings().download
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": YtdlLogger(),
        "skip_download": True,
        "socket_timeout": 30,
        "extractor_retries": 3,
        "noplaylist": not flat,
    }
    if flat:
        opts["extract_flat"] = "in_playlist"
        if max_items:
            opts["playlistend"] = max_items
    ffmpeg = get_ffmpeg_path()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg  # silences yt-dlp's "ffmpeg not found" warning
    js = get_js_runtimes()
    if js:
        opts["js_runtimes"] = js
    if dl.cookies_path.is_file():  # checked per call: saving cookies in the UI needs no restart
        opts["cookiefile"] = str(dl.cookies_path)
    if dl.cookies_from_browser:
        opts["cookiesfrombrowser"] = (dl.cookies_from_browser,)
    if dl.proxy:
        opts["proxy"] = dl.proxy
    return opts


def friendly_error(exc: BaseException) -> tuple[str, bool]:
    """
    Turn a yt-dlp exception into ``(message, retryable)``.

    Permanent problems (private / removed / region-locked / missing ffmpeg)
    are not retryable — retrying cannot help. Everything else that yt-dlp
    raised as a ``DownloadError`` (network resets, throttling) is.
    """
    text = _ANSI.sub("", str(exc)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    text = re.sub(r"^\[[\w:\-]+\]\s+[\w\-]{6,}:\s*", "", text)  # "[youtube] <id>: "
    lowered = text.lower()

    expected = False
    inner = None
    exc_info = getattr(exc, "exc_info", None)
    if exc_info and len(exc_info) > 1:
        inner = exc_info[1]
    if inner is not None and getattr(inner, "expected", False):
        expected = True

    permanent = expected or any(h in lowered for h in _NON_RETRYABLE_HINTS)
    is_download_error = type(exc).__name__ == "DownloadError"
    return (text[:500] or type(exc).__name__), (is_download_error and not permanent)
