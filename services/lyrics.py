"""
LyricsService — find lyrics for what the player is playing.

Provider: LRCLIB (``DL_LYRICS_URL``, default https://lrclib.net) — free, no API key, and it returns
*time-synced* lyrics (LRC) for many songs, which the immersive player highlights line by line.

YouTube titles are noisy ("Artist - Song (Official Video) [4K]"), so :func:`clean_title` turns
title + channel into ``(artist, track)`` first; an exact lookup (with the duration) is tried, then
a search. Results are cached in Redis (``P:LYRICS:<sha1>``): hits for a week, misses for a day.
Only the cleaned artist / track / duration ever leave the server — never anything about the user.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import requests

from configs import get_key_prefix, get_redis, get_settings
from models.lyrics import LyricLine, Lyrics
from modules import get_logger

logger = get_logger(__name__)

_HIT_TTL_S = 7 * 86400
_MISS_TTL_S = 86400
_TIMEOUT_S = 8
_USER_AGENT = "mst-ytdl (self-hosted downloader; lyrics lookup)"

# "(Official Video)", "[Lyrics]", "(feat. X)" … — anything in brackets that is not part of the song name
_BRACKETS = re.compile(
    r"\s*[\(\[\{][^)\]\}]*\b(official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|8k|remaster(ed)?|m/?v|"
    r"live|explicit|clean|colou?r coded|full song|feat\.?|ft\.?|prod\.?|from|version|edit)\b[^)\]\}]*[\)\]\}]",
    re.I,
)
_TRAILING_FEAT = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", re.I)
_CHANNEL_NOISE = re.compile(r"\s*(-\s*topic|vevo|official)\s*$", re.I)
_LRC_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_WORD_STAMP = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")  # enhanced LRC: per-word timing


def clean_title(title: str, uploader: str | None = None) -> tuple[str, str]:
    """
    ``("Artist - Song (Official Video)", "ArtistVEVO")`` → ``("Artist", "Song")``.

    Returns:
        ``(artist, track)``; ``artist`` may be '' when nothing usable is known.
    """
    t = _BRACKETS.sub("", title or "")
    t = re.split(r"\s+[|•]\s+", t)[0]  # "Song | Album Name" → "Song"
    t = t.replace("“", '"').replace("”", '"').strip(" -–—\"'")
    channel = _CHANNEL_NOISE.sub("", uploader or "").strip()
    artist, track = channel, t
    for sep in (" - ", " – ", " — "):
        if sep in t:
            left, right = t.split(sep, 1)
            artist, track = left.strip(), right.strip()
            break
    track = _TRAILING_FEAT.sub("", track).strip(" \"'")
    artist = _TRAILING_FEAT.sub("", artist).strip()
    return artist, track


def parse_lrc(text: str | None) -> list[LyricLine]:
    """LRC (``[mm:ss.xx] line``, several stamps per line allowed) → sorted lines."""
    out: list[LyricLine] = []
    for raw in (text or "").splitlines():
        stamps = list(_LRC_STAMP.finditer(raw))
        if not stamps:
            continue
        words = re.sub(r"\s{2,}", " ", _WORD_STAMP.sub("", _LRC_STAMP.sub("", raw))).strip()
        for m in stamps:
            secs = float(m.group(2).replace(":", "."))
            out.append(LyricLine(t=round(int(m.group(1)) * 60 + secs, 2), text=words))
    return sorted(out, key=lambda line: line.t)


class LyricsService:
    """Singleton: ``lyrics_service``."""

    @property
    def base_url(self) -> str | None:
        url = get_settings().download.lyrics_url
        return url.rstrip("/") if url else None

    def _http(self, path: str, params: dict[str, Any]) -> Any:
        """One provider call (isolated so tests can stub it). Returns parsed JSON, or None on 404."""
        r = requests.get(f"{self.base_url}{path}", params=params, timeout=_TIMEOUT_S,
                         headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _to_lyrics(rec: dict[str, Any]) -> Lyrics:
        synced = parse_lrc(rec.get("syncedLyrics"))
        plain = (rec.get("plainLyrics") or "").strip() or None
        return Lyrics(
            found=bool(synced or plain or rec.get("instrumental")),
            track=rec.get("trackName") or "",
            artist=rec.get("artistName") or "",
            synced=synced,
            plain=plain,
            instrumental=bool(rec.get("instrumental")),
            source="LRCLIB",
        )

    def _lookup(self, artist: str, track: str, duration: float | None) -> Lyrics:
        if artist and track:
            params: dict[str, Any] = {"artist_name": artist, "track_name": track}
            if duration:
                params["duration"] = round(duration)
            rec = self._http("/api/get", params)
            if rec:
                return self._to_lyrics(rec)
        results = self._http("/api/search", {"q": f"{artist} {track}".strip()}) or []
        if not isinstance(results, list) or not results:
            return Lyrics(source="LRCLIB", message="No lyrics found for this track")

        def score(rec: dict[str, Any]) -> tuple:
            off = abs((rec.get("duration") or 0) - duration) if duration else 0
            return (off > 5, not rec.get("syncedLyrics"), off)

        return self._to_lyrics(min(results, key=score))

    def find(self, title: str, artist: str | None = None, duration: float | None = None) -> Lyrics:
        """
        Lyrics for a track (cached).

        Args:
            title: the video / file title as shown in the player.
            artist: channel or artist name, if known.
            duration: seconds — picks the right version when several exist.

        Returns:
            A :class:`Lyrics`; ``found=False`` with a ``message`` when there are none (never raises
            for provider trouble — the player just shows the message).
        """
        if not self.base_url:
            return Lyrics(message="Lyrics are turned off on this server (DL_LYRICS_URL)")
        a, t = clean_title(title, artist)
        if not t:
            return Lyrics(message="No title to look up")
        key = f"{get_key_prefix()}:LYRICS:" + hashlib.sha1(
            f"{a.lower()}|{t.lower()}|{round(duration or 0)}".encode()
        ).hexdigest()
        r = get_redis()
        try:
            hit = r.get(key)
            if hit:
                return Lyrics.model_validate(json.loads(hit))
        except Exception:
            logger.debug("lyrics cache read failed", exc_info=True)
        try:
            out = self._lookup(a, t, duration)
        except (requests.RequestException, ValueError) as exc:
            logger.info(f"lyrics provider unavailable: {exc}")
            return Lyrics(message="The lyrics service is not reachable right now")
        try:
            r.set(key, out.model_dump_json(), ex=_HIT_TTL_S if out.found else _MISS_TTL_S)
        except Exception:
            logger.debug("lyrics cache write failed", exc_info=True)
        return out


lyrics_service = LyricsService()
