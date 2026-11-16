"""
LyricsService — find lyrics for what the player is playing.

Provider: LRCLIB (``DL_LYRICS_URL``, default https://lrclib.net) — free, no API key, and it returns
*time-synced* lyrics (LRC) for many songs, which the immersive player highlights line by line.

Optional target language (``lang``): ``auto`` keeps the provider text; anything else prefers a
same-script LRCLIB hit when search returns several, then machine-translates line-by-line (Google
Translate public endpoint, no key) so Hangul / Japanese / Chinese songs can show English (or
another listed language). Translations are cached in Redis with the original.

YouTube titles are noisy ("Artist - Song (Official Video) [4K]"), so :func:`clean_title` turns
title + channel into ``(artist, track)`` first; an exact lookup (with the duration) is tried, then
a search. Results are cached in Redis (``P:LYRICS:<sha1>``): hits for a week, misses for a day.
Only the cleaned artist / track / duration / language ever leave the server — never anything about the user.
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
_TRANSLATE_TIMEOUT_S = 12
_USER_AGENT = "mst-ytdl (self-hosted downloader; lyrics lookup)"
_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"

# Client / Settings picker — ``auto`` = leave LRCLIB text alone.
LYRIC_LANGS: tuple[str, ...] = (
    "auto", "en", "es", "fr", "de", "pt", "it", "ja", "ko", "zh-CN", "zh-TW",
    "hi", "ar", "ru", "th", "vi", "id", "tr",
)

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

# Script → ISO-ish family used to match LRCLIB hits and skip unnecessary translation.
_SCRIPTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ko", re.compile(r"[\uac00-\ud7af]")),
    ("ja", re.compile(r"[\u3040-\u30ff]")),  # kana → Japanese (CJK alone may still be zh)
    ("zh", re.compile(r"[\u4e00-\u9fff]")),
    ("ru", re.compile(r"[\u0400-\u04ff]")),
    ("ar", re.compile(r"[\u0600-\u06ff]")),
    ("hi", re.compile(r"[\u0900-\u097f]")),
    ("th", re.compile(r"[\u0e00-\u0e7f]")),
)
_LATIN = re.compile(r"[A-Za-z\u00C0-\u024F]")


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


def normalize_lang(lang: str | None) -> str:
    """Clamp a client language code to the supported set (default ``auto``)."""
    code = (lang or "auto").strip()
    if code in LYRIC_LANGS:
        return code
    # accept bare ``zh`` → Simplified
    low = code.lower().replace("_", "-")
    if low in {"zh", "zh-cn", "zh-hans"}:
        return "zh-CN"
    if low in {"zh-tw", "zh-hk", "zh-hant"}:
        return "zh-TW"
    for opt in LYRIC_LANGS:
        if opt.lower() == low:
            return opt
    return "auto"


def detect_lang(text: str) -> str:
    """
    Cheap script heuristic for lyric text (not a full language detector).

    Returns:
        A short code (``ko``, ``ja``, ``zh``, ``en``, …) or ``""`` when empty.
    """
    sample = (text or "")[:4000]
    if not sample.strip():
        return ""
    for code, pat in _SCRIPTS:
        if pat.search(sample):
            return code
    if _LATIN.search(sample):
        return "en"
    return ""


def _family(lang: str) -> str:
    """Collapse ``zh-CN`` / ``zh-TW`` → ``zh`` for script matching."""
    if lang.startswith("zh"):
        return "zh"
    return lang


def _sample_text(rec: dict[str, Any]) -> str:
    return (rec.get("syncedLyrics") or rec.get("plainLyrics") or "")[:2000]


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

    def _translate_http(self, text: str, target: str, source: str = "auto") -> tuple[str, str]:
        """
        Translate ``text`` to ``target``. Isolated for tests.

        Returns:
            ``(translated_text, detected_source_lang)``.
        """
        r = requests.get(
            _TRANSLATE_URL,
            params={"client": "gtx", "sl": source or "auto", "tl": target, "dt": "t", "q": text},
            timeout=_TRANSLATE_TIMEOUT_S,
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
        parts = data[0] or []
        out = "".join(seg[0] or "" for seg in parts if seg and seg[0] is not None)
        detected = ""
        if len(data) > 2 and isinstance(data[2], str):
            detected = data[2]
        return out, detected

    def _to_lyrics(
        self,
        rec: dict[str, Any],
        *,
        requested: str = "auto",
        translated: bool = False,
        language: str | None = None,
        source: str | None = None,
    ) -> Lyrics:
        synced = parse_lrc(rec.get("syncedLyrics"))
        plain = (rec.get("plainLyrics") or "").strip() or None
        sample = "\n".join(line.text for line in synced[:12]) if synced else (plain or "")
        lang = language if language is not None else detect_lang(sample)
        return Lyrics(
            found=bool(synced or plain or rec.get("instrumental")),
            track=rec.get("trackName") or "",
            artist=rec.get("artistName") or "",
            synced=synced,
            plain=plain,
            instrumental=bool(rec.get("instrumental")),
            source=source or "LRCLIB",
            language=lang,
            requested=requested,
            translated=translated,
        )

    def _score(self, rec: dict[str, Any], duration: float | None, prefer: str) -> tuple:
        """Lower is better. Prefer duration match, synced text, then the requested script."""
        off = abs((rec.get("duration") or 0) - duration) if duration else 0
        sample = _sample_text(rec)
        lang = detect_lang(sample)
        want = _family(prefer) if prefer and prefer != "auto" else ""
        # en → prefer latin / already-english; others → prefer matching script
        if want == "en":
            script_miss = 0 if lang in ("", "en") else 1
        elif want:
            script_miss = 0 if lang == want else 1
        else:
            script_miss = 0
        return (off > 5, not rec.get("syncedLyrics"), script_miss, off)

    def _wrong_script(self, rec: dict[str, Any], prefer: str) -> bool:
        want = _family(prefer) if prefer != "auto" else ""
        if not want:
            return False
        got = detect_lang(_sample_text(rec))
        if want == "en":
            return got not in ("", "en")
        return got != want

    def _lookup(self, artist: str, track: str, duration: float | None, prefer: str) -> Lyrics:
        candidates: list[dict[str, Any]] = []
        if artist and track:
            params: dict[str, Any] = {"artist_name": artist, "track_name": track}
            if duration:
                params["duration"] = round(duration)
            exact = self._http("/api/get", params)
            # Fast path: exact hit already matches the requested script (or lang=auto).
            if exact and not self._wrong_script(exact, prefer):
                return self._to_lyrics(exact, requested=prefer)
            if exact:
                candidates.append(exact)
        results = self._http("/api/search", {"q": f"{artist} {track}".strip()}) or []
        if isinstance(results, list):
            candidates.extend(results)
        if not candidates:
            return Lyrics(source="LRCLIB", message="No lyrics found for this track", requested=prefer)
        best = min(candidates, key=lambda r: self._score(r, duration, prefer))
        return self._to_lyrics(best, requested=prefer)

    def _translate_lyrics(self, lyrics: Lyrics, target: str) -> Lyrics:
        """Machine-translate synced lines (and plain text) to ``target``; keep timestamps."""
        if not lyrics.found or lyrics.instrumental and not lyrics.synced and not lyrics.plain:
            return lyrics
        src = lyrics.language or "auto"
        if _family(src) == _family(target) or (target == "en" and src in ("", "en")):
            return lyrics.model_copy(update={"requested": target, "translated": False})

        texts = [line.text for line in lyrics.synced] if lyrics.synced else []
        plain = lyrics.plain or ""
        # One request for synced lines (newline-joined); another for plain if needed.
        try:
            detected = src
            new_synced = list(lyrics.synced)
            if texts:
                blob = "\n".join(t if t.strip() else "·" for t in texts)  # keep empty slots
                translated, detected = self._translate_http(blob, target, source=src or "auto")
                parts = translated.split("\n")
                # Google sometimes collapses blanks — pad / trim to the line count.
                while len(parts) < len(texts):
                    parts.append("")
                new_synced = [
                    LyricLine(t=line.t, text=("" if src_t.strip() == "" else parts[i].replace("·", "").strip()))
                    for i, (line, src_t) in enumerate(zip(lyrics.synced, texts))
                ]
            new_plain = plain
            if plain and not texts:
                new_plain, detected = self._translate_http(plain, target, source=src or "auto")
                new_plain = new_plain.strip() or plain
            elif plain and texts:
                # Keep a plain mirror of the translated lines when we had plain before.
                new_plain = "\n".join(line.text for line in new_synced if line.text).strip() or plain
        except (requests.RequestException, ValueError, TypeError, IndexError) as exc:
            logger.info(f"lyrics translate failed: {exc}")
            return lyrics.model_copy(update={
                "requested": target,
                "message": "Translation unavailable — showing original lyrics",
            })

        return Lyrics(
            found=True,
            track=lyrics.track,
            artist=lyrics.artist,
            synced=new_synced,
            plain=new_plain or None,
            instrumental=lyrics.instrumental,
            source="LRCLIB · translated",
            language=_family(target) if target.startswith("zh") else target,
            requested=target,
            translated=True,
            message="",
        )

    def find(
        self,
        title: str,
        artist: str | None = None,
        duration: float | None = None,
        lang: str | None = "auto",
    ) -> Lyrics:
        """
        Lyrics for a track (cached).

        Args:
            title: the video / file title as shown in the player.
            artist: channel or artist name, if known.
            duration: seconds — picks the right version when several exist.
            lang: ``auto`` (original) or a code from :data:`LYRIC_LANGS` (prefer / translate).

        Returns:
            A :class:`Lyrics`; ``found=False`` with a ``message`` when there are none (never raises
            for provider trouble — the player just shows the message).
        """
        requested = normalize_lang(lang)
        if not self.base_url:
            return Lyrics(message="Lyrics are turned off on this server (DL_LYRICS_URL)", requested=requested)
        a, t = clean_title(title, artist)
        if not t:
            return Lyrics(message="No title to look up", requested=requested)
        key = f"{get_key_prefix()}:LYRICS:" + hashlib.sha1(
            f"{a.lower()}|{t.lower()}|{round(duration or 0)}|{requested}".encode()
        ).hexdigest()
        r = get_redis()
        try:
            hit = r.get(key)
            if hit:
                return Lyrics.model_validate(json.loads(hit))
        except Exception:
            logger.debug("lyrics cache read failed", exc_info=True)
        try:
            out = self._lookup(a, t, duration, requested)
            if out.found and requested != "auto":
                out = self._translate_lyrics(out, requested)
        except (requests.RequestException, ValueError) as exc:
            logger.info(f"lyrics provider unavailable: {exc}")
            return Lyrics(message="The lyrics service is not reachable right now", requested=requested)
        try:
            r.set(key, out.model_dump_json(), ex=_HIT_TTL_S if out.found else _MISS_TTL_S)
        except Exception:
            logger.debug("lyrics cache write failed", exc_info=True)
        return out


lyrics_service = LyricsService()
