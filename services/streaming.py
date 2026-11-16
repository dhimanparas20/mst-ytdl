"""
StreamService — play any YouTube video in the in-app player WITHOUT YouTube's iframe.

YouTube's embedded player refuses many videos (owner disabled embedding, region/age rules…).
yt-dlp can still read them, so instead of embedding we resolve the real CDN streams and serve
them through our own origin:

    info(video_id)                 formats → quality menu (+ audio-only, + subtitle tracks)
    open_media(video_id, fmt, rng) Range-capable proxy of ONE stream (browser <video>/<audio>)
    subtitle_vtt(video_id, lang)   a subtitle track as WebVTT

Design notes
    * The client never supplies an upstream URL — only a video id (validated) and a format id
      that must exist in the cached yt-dlp result. Upstream hosts are additionally restricted to
      googlevideo/youtube, so this cannot be turned into an open proxy (SSRF).
    * CDN URLs are signed for the requesting IP and expire (~6 h): results are cached ~20 min
      and dropped on a 403 so the next request re-resolves.
    * The CDN refuses small header ranges on some large streams and, for a second or two, even
      requests on a freshly signed URL. We use the ``&range=a-b`` query parameter (yt-dlp's own
      method) with the exact size from the URL's ``clen``, and retry 401/403 briefly before
      declaring the link expired. The browser's range is clamped to ≤ 8 MiB; a short 206 is valid
      and the browser simply asks again.
    * Above progressive 360p/720p YouTube only offers separate video and audio streams. The
      player plays those as two synchronised elements — see ``StreamInfo.qualities``.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from yt_dlp import YoutubeDL

from configs import get_settings
from models import QualityOption, StreamFormat, StreamInfo, SubtitleTrack
from modules import get_logger
from services.errors import InvalidRequestError, NotFoundError, RateLimitedError, UpstreamError
from services.ytdl_opts import active_cookiefile, base_extract_opts, cookie_scope_tag, friendly_error

logger = get_logger(__name__)

_ID_RE = re.compile(r"^[\w-]{11}$")
_CACHE_TTL_S = 20 * 60
_CACHE_MAX = 64
_CHUNK = 8 * 1024 * 1024  # upstream request size
_READ = 64 * 1024  # bytes per yield to the browser
_ATTEMPTS = 4  # tries for a CDN request that answers 401/403 right after the URL was signed
_RETRY_DELAY_S = 0.7
_HOST_OK = re.compile(r"(^|\.)(googlevideo\.com|youtube\.com|ytimg\.com|youtube-nocookie\.com)$")
_MIME = {
    ("video", "mp4"): "video/mp4", ("video", "webm"): "video/webm",
    ("muxed", "mp4"): "video/mp4", ("muxed", "webm"): "video/webm",
    ("audio", "m4a"): "audio/mp4", ("audio", "mp4"): "audio/mp4", ("audio", "webm"): "audio/webm",
    ("audio", "opus"): "audio/ogg",
}
_VTT_TAGS = re.compile(r"<\d{2}:\d{2}:\d{2}\.\d{3}>|</?c(?:\.[\w-]+)?>")


def _has(codec: str | None) -> bool:
    return bool(codec) and codec != "none"


@dataclass
class MediaStream:
    """An upstream response ready to be relayed to the browser."""

    status: int
    headers: dict[str, str]
    body: Iterator[bytes]


def _http_get(url: str, headers: dict[str, str], timeout: float = 20.0) -> requests.Response:
    """The only outbound HTTP call (isolated so tests can replace it)."""
    return requests.get(url, headers=headers, stream=True, timeout=timeout)


def _cookie_jar() -> MozillaCookieJar | None:
    """Netscape jar for timedtext.

    Prefers the active cookie scope (visitor / shared server). When the visitor has none
    (server mode with ``share_server_cookies`` off), falls back to the server jar — public
    captions are not account-private, and bare timedtext 429s without a login session.
    """
    path = active_cookiefile()
    if not path or not Path(path).is_file():
        p = get_settings().download.cookies_path
        path = str(p) if p.is_file() else None
    if not path:
        return None
    jar = MozillaCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except OSError:
        return None
    return jar


_SUB_ATTEMPTS = 3
_SUB_RETRY_S = 0.8
# Prefer formats the browser can use; json3 is a solid fallback when VTT is rate-limited.
_SUB_EXTS = ("vtt", "srt", "json3")


def _json3_to_vtt(text: str) -> str:
    """Convert YouTube's json3 timedtext payload into WebVTT."""
    import json

    data = json.loads(text)
    lines = ["WEBVTT", ""]
    for ev in data.get("events") or []:
        segs = ev.get("segs") or []
        body = "".join(s.get("utf8") or "" for s in segs).replace("\n", " ").strip()
        if not body or body == "\n":
            continue
        start = int(ev.get("tStartMs") or 0)
        dur = int(ev.get("dDurationMs") or 2000)
        end = start + max(dur, 200)

        def stamp(ms: int) -> str:
            h, rem = divmod(ms, 3_600_000)
            m, rem = divmod(rem, 60_000)
            s, milli = divmod(rem, 1000)
            return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"

        lines.append(f"{stamp(start)} --> {stamp(end)}")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def _srt_to_vtt(text: str) -> str:
    """Minimal SRT → WebVTT (comma millis → dot)."""
    body = text.lstrip("\ufeff")
    if body.startswith("WEBVTT"):
        return body
    body = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", body)
    return "WEBVTT\n\n" + body


class StreamService:
    """Singleton: ``stream_service``."""

    def __init__(self) -> None:
        self._cache: OrderedDict[str, tuple[float, dict[str, Any], StreamInfo]] = OrderedDict()
        self._lock = threading.Lock()

    # ── yt-dlp ──────────────────────────────────────────────────────

    def _extract(self, video_id: str) -> dict[str, Any]:
        """Resolve formats + subtitles (isolated for tests). Slow: ~5–8 s (JS challenge solving)."""
        with YoutubeDL(base_extract_opts()) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        if not info:
            raise InvalidRequestError("yt-dlp returned no information for this video")
        return info

    # ── cache ───────────────────────────────────────────────────────

    def _get(self, video_id: str) -> tuple[dict[str, Any], StreamInfo]:
        if not _ID_RE.match(video_id or ""):
            raise InvalidRequestError("That is not a valid YouTube video id")
        now = time.monotonic()
        tag = cookie_scope_tag()  # server mode: a visitor's own cookies get their own entries
        key = f"{video_id}|{tag}" if tag else video_id
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < _CACHE_TTL_S:
                self._cache.move_to_end(key)
                return hit[1], hit[2]
        try:
            raw = self._extract(video_id)
        except InvalidRequestError:
            raise
        except Exception as exc:  # yt-dlp DownloadError & friends
            raise InvalidRequestError(friendly_error(exc)[0]) from exc
        info = self.build_info(video_id, raw)
        with self._lock:
            self._cache[key] = (time.monotonic(), raw, info)
            while len(self._cache) > _CACHE_MAX:
                self._cache.popitem(last=False)
        return raw, info

    def clear(self) -> None:
        """Drop every cached entry (e.g. after the cookies changed)."""
        with self._lock:
            self._cache.clear()

    def forget(self, video_id: str) -> None:
        """Drop a cached entry (its signed URLs went stale)."""
        with self._lock:
            for k in [k for k in self._cache if k.split("|", 1)[0] == video_id]:
                self._cache.pop(k, None)

    # ── public API ──────────────────────────────────────────────────

    def info(self, video_id: str) -> StreamInfo:
        """Qualities, audio-only stream and subtitle tracks of ``video_id``."""
        return self._get(video_id)[1]

    @staticmethod
    def build_info(video_id: str, raw: dict[str, Any]) -> StreamInfo:
        """Turn a raw yt-dlp result into the player's view (pure; unit-tested)."""
        usable: list[dict[str, Any]] = []
        for f in raw.get("formats") or []:
            if not f.get("url") or f.get("protocol") not in ("https", "http"):
                continue  # manifests / fragment lists can't be proxied as one ranged file
            if f.get("ext") in ("mhtml", "3gp") or f.get("format_note") == "storyboard":
                continue
            usable.append(f)

        def kind(f: dict[str, Any]) -> str | None:
            v, a = _has(f.get("vcodec")), _has(f.get("acodec"))
            return "muxed" if v and a else "video" if v else "audio" if a else None

        formats: list[StreamFormat] = []
        for f in usable:
            k = kind(f)
            if not k:
                continue
            h = f.get("height")
            fps = f.get("fps")
            label = (
                f"{h}p{int(fps)}" if k != "audio" and h and fps and fps > 30 else f"{h}p" if k != "audio" and h
                else f"{round(f.get('abr') or 0)} kbps {f.get('ext', '')}".strip()
            )
            formats.append(
                StreamFormat(
                    format_id=str(f["format_id"]), kind=k, label=label, height=h, fps=fps, ext=f.get("ext", ""),
                    vcodec=f.get("vcodec") if _has(f.get("vcodec")) else None,
                    acodec=f.get("acodec") if _has(f.get("acodec")) else None,
                    abr=f.get("abr"), filesize=f.get("filesize") or f.get("filesize_approx"),
                )
            )

        audios = [f for f in usable if kind(f) == "audio"]
        # m4a first (every browser plays it), then by bitrate
        audios.sort(key=lambda f: (f.get("ext") == "m4a", f.get("abr") or 0), reverse=True)
        best_audio = str(audios[0]["format_id"]) if audios else None

        def pick(candidates: list[dict[str, Any]]) -> dict[str, Any]:
            # H.264 plays everywhere; otherwise VP9 before AV1 (cheaper to decode); then bitrate
            def rank(f: dict[str, Any]) -> tuple:
                vc = f.get("vcodec") or ""
                return (vc.startswith("avc1"), vc.startswith("vp9"), f.get("tbr") or 0)

            return max(candidates, key=rank)

        qualities: list[QualityOption] = []
        heights = sorted({f["height"] for f in usable if kind(f) in ("muxed", "video") and f.get("height")}, reverse=True)
        for h in heights:
            muxed = [f for f in usable if kind(f) == "muxed" and f.get("height") == h]
            video = [f for f in usable if kind(f) == "video" and f.get("height") == h]
            if muxed:  # single file wins: no sync needed
                f = pick(muxed)
                qualities.append(QualityOption(height=h, label=f"{h}p", video_format=str(f["format_id"]), muxed=True))
            elif video and best_audio:
                f = pick(video)
                fps = f.get("fps")
                qualities.append(
                    QualityOption(height=h, label=f"{h}p{int(fps)}" if fps and fps > 30 else f"{h}p",
                                  video_format=str(f["format_id"]), audio_format=best_audio)
                )

        subs: list[SubtitleTrack] = []
        manual = raw.get("subtitles") or {}
        for lang, tracks in list(manual.items())[:30]:
            if lang != "live_chat" and tracks:
                subs.append(SubtitleTrack(lang=lang, name=tracks[0].get("name") or lang))
        auto = raw.get("automatic_captions") or {}
        wanted = {"en", raw.get("language")} | {k.removesuffix("-orig") for k in auto if k.endswith("-orig")}
        for lang, tracks in auto.items():
            base = lang.removesuffix("-orig")
            if tracks and (lang.endswith("-orig") or lang in wanted) and not any(s.lang == base and not s.auto for s in subs):
                if not any(s.lang == lang and s.auto for s in subs):
                    subs.append(SubtitleTrack(lang=lang, name=f"{tracks[0].get('name') or base} (auto)", auto=True))

        return StreamInfo(
            video_id=video_id, title=(raw.get("title") or "").strip(), uploader=raw.get("uploader") or raw.get("channel"),
            duration=raw.get("duration"), thumbnail=raw.get("thumbnail"), qualities=qualities,
            audio_format=best_audio, formats=formats, subtitles=subs,
            live=bool(raw.get("is_live") or raw.get("live_status") == "is_live"),
        )

    # ── media proxy ─────────────────────────────────────────────────

    @staticmethod
    def parse_range(header: str | None, total: int | None) -> tuple[int, int | None]:
        """
        ``"bytes=100-"`` → ``(100, None)``; ``"bytes=100-199"`` → ``(100, 199)``;
        ``"bytes=-500"`` (last 500) → needs ``total``. No header → ``(0, None)``.

        Raises:
            InvalidRequestError: malformed or unsatisfiable.
        """
        if not header:
            return 0, None
        m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header)
        if not m or (not m.group(1) and not m.group(2)):
            raise InvalidRequestError("Bad Range header")
        if not m.group(1):  # suffix range
            if not total:
                return 0, None
            n = int(m.group(2))
            return max(total - n, 0), total - 1
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else None
        if end is not None and end < start:
            raise InvalidRequestError("Bad Range header")
        return start, end

    def _format(self, video_id: str, format_id: str) -> tuple[dict[str, Any], StreamFormat]:
        raw, info = self._get(video_id)
        meta = next((f for f in info.formats if f.format_id == format_id), None)
        src = next((f for f in raw.get("formats") or [] if str(f.get("format_id")) == format_id), None)
        if not meta or not src:
            raise InvalidRequestError(f"Unknown format {format_id!r} for this video")
        host = urlparse(src["url"]).hostname or ""
        if not _HOST_OK.search(host):
            raise InvalidRequestError("Refusing to proxy a non-YouTube host")
        return src, meta

    @staticmethod
    def _total_size(src: dict[str, Any]) -> int | None:
        """Exact byte size: the URL's own ``clen`` field wins, then yt-dlp's ``filesize`` (never an estimate)."""
        clen = parse_qs(urlparse(src["url"]).query).get("clen", [""])[0]
        if clen.isdigit():
            return int(clen)
        return src.get("filesize") or None

    def open_media(self, video_id: str, format_id: str, range_header: str | None) -> MediaStream:
        """
        Relay one ranged chunk of a stream.

        Returns:
            ``MediaStream`` with status 206 (always — a partial answer is valid even when the
            browser sent no Range), ``Content-Range``, ``Content-Length`` and the byte iterator.

        Raises:
            InvalidRequestError: bad id / format / range.
            UpstreamError: the CDN refused (stale signature) or was unreachable.
        """
        src, meta = self._format(video_id, format_id)
        total = self._total_size(src)
        start, end = self.parse_range(range_header, total)
        if total and start >= total:
            raise InvalidRequestError("Range starts past the end of the stream")
        # How many bytes we hand the browser this time (clamped; it simply asks again for more).
        want = _CHUNK if end is None else min(end - start + 1, _CHUNK)
        if total:
            want = min(want, total - start)

        # How we ask the CDN matters. Small header ranges ("Range: bytes=0-…") are refused on some large
        # adaptive streams, while the `&range=a-b` query parameter — what yt-dlp's own downloader uses —
        # works. So: query parameter when the exact size is known; otherwise the header form (plus one
        # plain, range-less retry for byte 0).
        headers = dict(src.get("http_headers") or {})
        url = src["url"]
        if total:
            url += ("&" if "?" in url else "?") + f"range={start}-{start + want - 1}"
        else:
            headers["Range"] = f"bytes={start}-"
        try:
            # A freshly signed URL is refused (401/403) for its first second or two — retry briefly before
            # concluding that the link really expired.
            plain_fallback = not total and start == 0  # header-range form: a plain GET is the better retry
            attempts = 1 if plain_fallback else _ATTEMPTS
            for attempt in range(attempts):
                resp = _http_get(url, headers)
                if resp.status_code not in (401, 403) or attempt == attempts - 1:
                    break
                resp.close()
                time.sleep(_RETRY_DELAY_S)
            if resp.status_code == 403 and plain_fallback:
                resp.close()
                resp = _http_get(src["url"], {k: v for k, v in headers.items() if k != "Range"})
        except requests.RequestException as exc:
            raise UpstreamError(f"Could not reach YouTube's CDN: {exc}") from exc
        if resp.status_code in (401, 403, 404, 410):
            logger.warning(f"CDN refused {video_id}/{format_id}: HTTP {resp.status_code} for range {start}+{want} "
                           f"(host {urlparse(url).hostname}, params {sorted(parse_qs(urlparse(url).query))}, "
                           f"headers {sorted(headers)}, UA {headers.get('User-Agent', '')[:40]})")
            resp.close()
            self.forget(video_id)  # signature expired → re-resolve on the next request
            raise UpstreamError("The stream link expired — press play again")
        if resp.status_code not in (200, 206):
            resp.close()
            raise UpstreamError(f"YouTube's CDN answered HTTP {resp.status_code}")

        length = int(resp.headers.get("Content-Length") or 0)
        crange = resp.headers.get("Content-Range", "")
        m = re.search(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", crange)
        if total:  # the slice was selected by the query parameter: it starts exactly at `start`
            if length and length > want and start:  # parameter ignored → a whole file, not our slice
                resp.close()
                raise UpstreamError("The CDN ignored the range request")
            first, upstream_last, real_total = start, start + want - 1, total
        elif resp.status_code == 206 and m:
            first, upstream_last = int(m.group(1)), int(m.group(2))
            real_total = int(m.group(3)) if m.group(3) != "*" else None
        else:  # server ignored Range and sent the whole file from byte 0
            if start:
                resp.close()
                raise UpstreamError("The CDN ignored the Range request")
            first, upstream_last, real_total = 0, (length or 1) - 1, length or None
        last = min(first + want - 1, upstream_last)
        out = {
            "Content-Type": _MIME.get((meta.kind, meta.ext), "application/octet-stream"),
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {first}-{last}/{real_total if real_total else '*'}",
            "Content-Length": str(last - first + 1),
            "Cache-Control": "private, max-age=300",
        }
        to_send = last - first + 1

        def body() -> Iterator[bytes]:
            sent = 0
            try:
                for piece in resp.iter_content(_READ):
                    if sent + len(piece) >= to_send:  # hit our clamp: send the tail and stop reading
                        yield piece[: to_send - sent]
                        return
                    sent += len(piece)
                    yield piece
            except requests.RequestException:  # connection dropped mid-chunk: the browser re-requests
                return
            finally:
                resp.close()

        return MediaStream(206, out, body())

    # ── subtitles ───────────────────────────────────────────────────

    def subtitle_vtt(self, video_id: str, lang: str, auto: bool = False) -> str:
        """A subtitle track as clean WebVTT text.

        Raises:
            InvalidRequestError: no such track / not a YouTube host.
            RateLimitedError: YouTube rate-limited the timedtext endpoint.
            NotFoundError: track listed but nothing usable came back.
            UpstreamError: network failure reaching YouTube.
        """
        raw, _ = self._get(video_id)
        tracks = (raw.get("automatic_captions") if auto else raw.get("subtitles")) or {}
        entries = tracks.get(lang) or []
        by_ext = {e.get("ext"): e for e in entries if e.get("url")}
        picks = [by_ext[ext] for ext in _SUB_EXTS if ext in by_ext]
        if not picks:
            raise InvalidRequestError(f"No WebVTT subtitles for '{lang}'")

        headers = dict(raw.get("http_headers") or {})
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )
        jar = _cookie_jar()  # timedtext 429s without a login session (verified live)
        last_status = 0
        saw_429 = False

        for pick in picks:
            host = urlparse(pick["url"]).hostname or ""
            if not _HOST_OK.search(host):
                raise InvalidRequestError("Refusing to fetch a non-YouTube host")
            for attempt in range(_SUB_ATTEMPTS):
                try:
                    r = requests.get(pick["url"], timeout=20, headers=headers, cookies=jar)
                except requests.RequestException as exc:
                    raise UpstreamError(f"Could not fetch subtitles: {exc}") from exc
                last_status = r.status_code
                if r.status_code == 429:
                    saw_429 = True
                    if attempt < _SUB_ATTEMPTS - 1:
                        time.sleep(_SUB_RETRY_S * (attempt + 1))
                        continue
                    break
                if r.status_code != 200 or not r.text.strip():
                    break
                ext = pick.get("ext")
                text = r.text
                try:
                    if ext == "vtt" or text.lstrip().startswith("WEBVTT"):
                        if "WEBVTT" not in text[:20]:
                            break
                        return clean_vtt(text)
                    if ext == "srt":
                        return clean_vtt(_srt_to_vtt(text))
                    if ext == "json3":
                        return clean_vtt(_json3_to_vtt(text))
                except (ValueError, KeyError, TypeError):
                    break
                break

        if saw_429:
            raise RateLimitedError("YouTube rate-limited subtitles — try again in a moment")
        if last_status in (401, 403, 404):
            raise NotFoundError("Those subtitles are not available right now")
        raise NotFoundError("YouTube returned no usable subtitles")


def clean_vtt(text: str) -> str:
    """Strip YouTube's karaoke timing tags (``<00:00:01.200><c>word</c>``) so cues render as plain lines."""
    return _VTT_TAGS.sub("", text)


stream_service = StreamService()
