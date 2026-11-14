"""services/streaming.py + api/routers/stream.py — everything except the real network."""

from __future__ import annotations

import subprocess
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from fastapi.testclient import TestClient

import services.streaming as st
from api.main import create_app
from configs import get_ffmpeg_path
from services import InvalidRequestError, NotFoundError, UpstreamError
from services import library_service as lib
from services import stream_service as svc
from services.streaming import StreamService, clean_vtt

VID = "dQw4w9WgXcQ"
CDN = "https://rr1---sn-abc.googlevideo.com/videoplayback?id=1"


def fmt(fid, *, v="none", a="none", ext="mp4", h=None, fps=30, abr=None, tbr=None, size=None, proto="https", url=CDN):
    return {"format_id": fid, "vcodec": v, "acodec": a, "ext": ext, "height": h, "fps": fps, "abr": abr, "tbr": tbr,
            "filesize": size, "protocol": proto, "url": url, "http_headers": {"User-Agent": "UA-test"}}


def raw_video(**kw):
    formats = [
        fmt("18", v="avc1.42001E", a="mp4a.40.2", h=360, tbr=500, size=3_000_000),            # muxed 360p
        fmt("22", v="avc1.64001F", a="mp4a.40.2", h=720, tbr=1200, size=9_000_000),           # muxed 720p
        fmt("137", v="avc1.640028", h=1080, tbr=4000, size=20_000_000),                       # video-only 1080p avc1
        fmt("248", v="vp9", ext="webm", h=1080, tbr=3000, size=15_000_000),                   # video-only 1080p vp9
        fmt("313", v="vp09.00.50", ext="webm", h=2160, fps=60, tbr=16000, size=90_000_000),   # 4K60 vp9
        fmt("160", v="avc1.4d400c", h=144, tbr=100, size=1_000_000),                          # video-only 144p
        fmt("140", a="mp4a.40.2", ext="m4a", abr=129.5, size=3_400_000),                      # audio m4a
        fmt("251", a="opus", ext="webm", abr=140.0, size=3_300_000),                          # audio opus
        fmt("139", a="mp4a.40.5", ext="m4a", abr=48.0, size=1_200_000),                       # audio m4a low
        fmt("hls-1", v="avc1", a="mp4a", h=480, proto="m3u8_native"),                          # manifest → unusable
        fmt("sb0", v="none", ext="mhtml", h=90),                                              # storyboard
        {"format_id": "nourl", "vcodec": "avc1", "acodec": "none", "ext": "mp4", "height": 240, "protocol": "https"},
    ]
    return {"id": VID, "title": " Song ", "uploader": "Artist", "duration": 212, "thumbnail": "http://t/x.jpg",
            "formats": formats, "subtitles": {}, "automatic_captions": {}, **kw}


class TestBuildInfo:
    def info(self, **kw):
        return StreamService.build_info(VID, raw_video(**kw))

    def test_basic_fields(self):
        i = self.info()
        assert (i.video_id, i.title, i.uploader, i.duration) == (VID, "Song", "Artist", 212)

    def test_unusable_formats_are_dropped(self):
        ids = {f.format_id for f in self.info().formats}
        assert "hls-1" not in ids and "sb0" not in ids and "nourl" not in ids
        assert {"18", "22", "137", "248", "313", "160", "140", "251", "139"} <= ids

    def test_qualities_sorted_best_first_with_correct_wiring(self):
        q = {o.height: o for o in self.info().qualities}
        assert list(q) == [2160, 1080, 720, 360, 144]
        assert q[720].muxed and q[720].video_format == "22" and q[720].audio_format is None
        assert q[360].muxed and q[360].video_format == "18"
        assert not q[1080].muxed and q[1080].video_format == "137"  # H.264 preferred over VP9
        assert q[1080].audio_format == "140"                          # best m4a
        assert q[2160].label == "2160p60" and q[2160].video_format == "313"
        assert q[144].audio_format == "140"

    def test_audio_only_prefers_m4a_over_a_higher_bitrate_opus(self):
        assert self.info().audio_format == "140"

    def test_no_audio_stream_means_no_split_qualities(self):
        raw = raw_video()
        raw["formats"] = [f for f in raw["formats"] if f["acodec"] == "none" and f["vcodec"] != "none" or f["format_id"] in ("18",)]
        i = StreamService.build_info(VID, raw)
        assert [o.height for o in i.qualities] == [360] and i.audio_format is None

    def test_vp9_beats_av1_when_there_is_no_h264(self):
        raw = raw_video()
        raw["formats"] = [fmt("a1", v="av01.0.08M.08", ext="mp4", h=720, tbr=9999), fmt("v9", v="vp9", ext="webm", h=720, tbr=10),
                          fmt("140", a="mp4a", ext="m4a", abr=128)]
        assert StreamService.build_info(VID, raw).qualities[0].video_format == "v9"

    def test_formats_carry_labels_and_sizes(self):
        by = {f.format_id: f for f in self.info().formats}
        assert by["18"].label == "360p" and by["18"].kind == "muxed" and by["18"].filesize == 3_000_000
        assert by["140"].kind == "audio" and by["140"].label == "130 kbps m4a" or by["140"].label.endswith("m4a")
        assert by["313"].label == "2160p60"

    def test_live_flag(self):
        assert self.info(is_live=True).live is True and self.info().live is False

    def test_subtitles_manual_and_selected_auto(self):
        i = self.info(
            subtitles={"en": [{"ext": "vtt", "name": "English"}], "hi": [{"ext": "vtt", "name": "Hindi"}], "live_chat": [{"ext": "json"}]},
            automatic_captions={"en": [{"ext": "vtt", "name": "English"}], "de": [{"ext": "vtt"}], "fr-orig": [{"ext": "vtt", "name": "French"}]},
            language="fr",
        )
        got = {(s.lang, s.auto) for s in i.subtitles}
        assert ("en", False) in got and ("hi", False) in got
        assert ("live_chat", False) not in got
        assert ("en", True) not in got                      # a manual 'en' track exists → no duplicate auto 'en'
        assert ("fr-orig", True) in got and ("de", True) not in got  # only original-language auto captions

    def test_auto_en_is_offered_when_there_is_no_manual_en(self):
        i = self.info(automatic_captions={"en": [{"ext": "vtt", "name": "English"}]})
        assert [(s.lang, s.auto, s.name) for s in i.subtitles] == [("en", True, "English (auto)")]

    def test_empty_result_does_not_crash(self):
        i = StreamService.build_info(VID, {"title": "x"})
        assert i.qualities == [] and i.formats == [] and i.audio_format is None


class TestParseRange:
    @pytest.mark.parametrize("hdr,total,expected", [
        (None, 1000, (0, None)), ("", 1000, (0, None)),
        ("bytes=0-", 1000, (0, None)), ("bytes=100-", 1000, (100, None)), ("bytes=100-199", 1000, (100, 199)),
        ("bytes=-200", 1000, (800, 999)), ("bytes=-5000", 1000, (0, 999)), ("bytes=-200", None, (0, None)),
        (" bytes = 5 - 9 ", 1000, (5, 9)),
    ])
    def test_valid(self, hdr, total, expected):
        assert StreamService.parse_range(hdr, total) == expected

    @pytest.mark.parametrize("hdr", ["bytes=-", "bytes=9-5", "items=0-5", "bytes=abc", "0-5"])
    def test_invalid(self, hdr):
        with pytest.raises(InvalidRequestError):
            StreamService.parse_range(hdr, 1000)


class FakeResp:
    def __init__(self, status=206, headers=None, body=b"x" * 100):
        self.status_code, self.headers, self._body, self.closed = status, headers or {}, body, False

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def close(self):
        self.closed = True

    @property
    def text(self):
        return self._body.decode()


@pytest.fixture
def served(monkeypatch):
    """A cached video + a scripted CDN; returns the list of upstream calls."""
    svc._cache.clear()
    monkeypatch.setattr(svc, "_extract", lambda vid: raw_video())
    class Calls(list):
        """Upstream request log + CDN quirk switches.

        ``refuse_first``      the first N requests answer 403 (a freshly signed URL "warming up")
        ``refuse_zero_range`` header ranges starting at byte 0 answer 403 until a plain GET happened
        """

        refuse_first = 0
        refuse_zero_range = False

        def set_total(self, n: int) -> None:
            calls_total[0] = n

    calls = Calls()
    warm = {"url": False}
    monkeypatch.setattr(st, "_RETRY_DELAY_S", 0)  # no real sleeping in unit tests

    def fake_get(url, headers, timeout=20.0):
        """Behaves like googlevideo: `&range=a-b` returns exactly that slice (200)."""
        calls.append((url, dict(headers)))
        if len(calls) <= calls.refuse_first:
            return FakeResp(403)
        q = parse_qs(urlparse(url).query)
        if "range" in q:
            a, b = (int(x) for x in q["range"][0].split("-"))
            return FakeResp(200, {"Content-Length": str(b - a + 1)}, b"v" * (b - a + 1))
        total = calls_total[0]
        rng = headers.get("Range")
        if not rng:  # plain GET: the whole file, and it "activates" the URL
            warm["url"] = True
            return FakeResp(200, {"Content-Length": str(total)}, b"v" * total)
        a = int(rng.split("=")[1].split("-")[0])
        if a == 0 and calls.refuse_zero_range and not warm["url"]:
            return FakeResp(403)
        return FakeResp(206, {"Content-Range": f"bytes {a}-{total - 1}/{total}", "Content-Length": str(total - a)}, b"v" * (total - a))

    calls_total = [3_000_000]
    monkeypatch.setattr(st, "_http_get", fake_get)
    yield calls
    svc._cache.clear()


class TestOpenMedia:
    def test_first_request_is_chunked_and_answered_206(self, served):
        m = svc.open_media(VID, "18", "bytes=0-")
        assert m.status == 206
        url, headers = served[0]
        assert "range=0-2999999" in url and "Range" not in headers  # the query-parameter form, not a header
        assert m.headers["Content-Range"] == "bytes 0-2999999/3000000" and m.headers["Content-Type"] == "video/mp4"
        assert m.headers["Accept-Ranges"] == "bytes" and int(m.headers["Content-Length"]) == 3_000_000
        assert len(b"".join(m.body)) == 3_000_000

    def test_big_streams_are_served_in_8mib_chunks(self, served):
        m = svc.open_media(VID, "313", "bytes=0-")  # 90 MB stream
        eight = 8 * 1024 * 1024
        assert f"range=0-{eight - 1}" in served[0][0]
        assert m.headers["Content-Range"] == f"bytes 0-{eight - 1}/90000000"
        assert int(m.headers["Content-Length"]) == eight
        assert len(b"".join(m.body)) == eight

    def test_small_closed_range_from_byte_zero_uses_the_query_parameter(self, served):
        """Regression: the CDN 403s small header ranges on big streams; the query parameter works."""
        served.refuse_zero_range = True
        m = svc.open_media(VID, "140", "bytes=0-65535")
        assert len(served) == 1 and "range=0-65535" in served[0][0] and "Range" not in served[0][1]
        assert m.headers["Content-Range"] == "bytes 0-65535/3400000" and int(m.headers["Content-Length"]) == 65536
        assert len(b"".join(m.body)) == 65536

    @pytest.mark.parametrize("refusals", [1, 2, 3])
    def test_a_freshly_signed_url_that_is_refused_at_first_is_retried(self, served, refusals):
        """Regression (seen live): the first requests on a just-extracted URL answer 403."""
        served.refuse_first = refusals
        m = svc.open_media(VID, "18", "bytes=0-99")
        assert len(served) == refusals + 1 and len(b"".join(m.body)) == 100
        assert VID in svc._cache  # the cache entry survived: no needless re-extraction

    def test_persistent_refusal_gives_up_after_the_attempts_and_drops_the_cache(self, served):
        served.refuse_first = 99
        with pytest.raises(UpstreamError, match="expired"):
            svc.open_media(VID, "18", "bytes=0-99")
        assert len(served) == st._ATTEMPTS and VID not in svc._cache

    def test_404_and_410_are_not_retried(self, served, monkeypatch):
        n = {"c": 0}
        monkeypatch.setattr(st, "_http_get", lambda *a, **k: n.__setitem__("c", n["c"] + 1) or FakeResp(410))
        with pytest.raises(UpstreamError):
            svc.open_media(VID, "18", None)
        assert n["c"] == 1

    def test_seek_in_the_middle(self, served):
        m = svc.open_media(VID, "18", "bytes=1000000-1000999")
        assert "range=1000000-1000999" in served[0][0]
        assert m.headers["Content-Range"] == "bytes 1000000-1000999/3000000"
        assert len(b"".join(m.body)) == 1000

    def test_suffix_range(self, served):
        m = svc.open_media(VID, "18", "bytes=-1000")
        assert "range=2999000-2999999" in served[0][0]
        assert m.headers["Content-Range"] == "bytes 2999000-2999999/3000000"

    def test_exact_size_comes_from_the_url_clen_not_an_estimate(self, served, monkeypatch):
        svc._cache.clear()
        r = raw_video()
        r["formats"][0]["url"] = CDN + "&clen=1234"  # format 18; filesize says 3_000_000
        monkeypatch.setattr(svc, "_extract", lambda vid: r)
        m = svc.open_media(VID, "18", "bytes=0-")
        assert m.headers["Content-Range"] == "bytes 0-1233/1234" and int(m.headers["Content-Length"]) == 1234

    def test_unknown_size_falls_back_to_header_ranges_and_survives_the_fresh_url_403(self, served, monkeypatch):
        svc._cache.clear()
        r = raw_video()
        for f in r["formats"]:
            f["filesize"] = None  # no size known anywhere
        monkeypatch.setattr(svc, "_extract", lambda vid: r)
        served.set_total(5_000)
        served.refuse_zero_range = True
        m = svc.open_media(VID, "18", "bytes=0-99")
        assert [h.get("Range") for _, h in served] == ["bytes=0-", None]  # header try → 403 → plain retry
        assert m.headers["Content-Range"] == "bytes 0-99/5000" and len(b"".join(m.body)) == 100
        m2 = svc.open_media(VID, "18", "bytes=1000-1099")  # URL is warm now: header range is fine
        assert served[-1][1]["Range"] == "bytes=1000-"
        assert m2.headers["Content-Range"] == "bytes 1000-1099/5000" and len(b"".join(m2.body)) == 100

    def test_no_range_header_behaves_like_zero_dash(self, served):
        assert svc.open_media(VID, "18", None).status == 206 and "range=0-" in served[0][0]

    def test_per_format_http_headers_are_forwarded(self, served):
        svc.open_media(VID, "18", None)
        assert served[0][1]["User-Agent"] == "UA-test"

    def test_audio_and_webm_mime_types(self, served):
        assert svc.open_media(VID, "140", "bytes=0-9").headers["Content-Type"] == "audio/mp4"
        assert svc.open_media(VID, "251", "bytes=0-9").headers["Content-Type"] == "audio/webm"
        assert svc.open_media(VID, "248", "bytes=0-9").headers["Content-Type"] == "video/webm"

    def test_range_past_the_end_is_rejected(self, served):
        with pytest.raises(InvalidRequestError, match="past the end"):
            svc.open_media(VID, "18", "bytes=9000000-")

    @pytest.mark.parametrize("vid", ["short", "", "../../etc/passwd", "a" * 12, "dQw4w9WgXc!"])
    def test_bad_video_ids(self, served, vid):
        with pytest.raises(InvalidRequestError):
            svc.open_media(vid, "18", None)
        assert served == []

    @pytest.mark.parametrize("fid", ["999", "nourl", "hls-1", "", "18; rm -rf"])
    def test_unknown_or_unusable_formats(self, served, fid):
        with pytest.raises(InvalidRequestError):
            svc.open_media(VID, fid, None)

    def test_client_cannot_make_us_fetch_a_foreign_host(self, monkeypatch):
        svc._cache.clear()
        evil = raw_video()
        evil["formats"][0]["url"] = "http://169.254.169.254/latest/meta-data"
        monkeypatch.setattr(svc, "_extract", lambda vid: evil)
        monkeypatch.setattr(st, "_http_get", lambda *a, **k: pytest.fail("outbound call to a non-YouTube host"))
        with pytest.raises(InvalidRequestError, match="non-YouTube"):
            svc.open_media(VID, "18", None)
        svc._cache.clear()

    @pytest.mark.parametrize("host", ["googlevideo.com", "rr1---sn-x.googlevideo.com", "www.youtube.com", "i.ytimg.com"])
    def test_allowed_hosts(self, host, monkeypatch):
        svc._cache.clear()
        r = raw_video()
        r["formats"][0]["url"] = f"https://{host}/x"
        monkeypatch.setattr(svc, "_extract", lambda vid: r)
        monkeypatch.setattr(st, "_http_get", lambda *a, **k: FakeResp(206, {"Content-Range": "bytes 0-9/3000000", "Content-Length": "10"}, b"x" * 10))
        assert svc.open_media(VID, "18", "bytes=0-9").status == 206
        svc._cache.clear()

    @pytest.mark.parametrize("host", ["evilgooglevideo.com", "googlevideo.com.evil.io", "example.com", "localhost"])
    def test_lookalike_hosts_are_refused(self, host, monkeypatch):
        svc._cache.clear()
        r = raw_video()
        r["formats"][0]["url"] = f"https://{host}/x"
        monkeypatch.setattr(svc, "_extract", lambda vid: r)
        with pytest.raises(InvalidRequestError):
            svc.open_media(VID, "18", None)
        svc._cache.clear()

    @pytest.mark.parametrize("code", [401, 403, 404, 410])
    def test_expired_signature_drops_the_cache(self, served, monkeypatch, code):
        monkeypatch.setattr(st, "_http_get", lambda *a, **k: FakeResp(code))
        svc.info(VID)
        assert VID in svc._cache
        with pytest.raises(UpstreamError, match="expired"):
            svc.open_media(VID, "18", None)
        assert VID not in svc._cache  # next request re-resolves fresh URLs

    def test_other_upstream_errors(self, served, monkeypatch):
        monkeypatch.setattr(st, "_http_get", lambda *a, **k: FakeResp(503))
        with pytest.raises(UpstreamError, match="503"):
            svc.open_media(VID, "18", None)

    def test_network_failure(self, served, monkeypatch):
        def boom(*a, **k):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(st, "_http_get", boom)
        with pytest.raises(UpstreamError, match="Could not reach"):
            svc.open_media(VID, "18", None)

    def test_a_cdn_that_ignores_the_range_parameter_is_cut_to_the_slice_at_byte_zero(self, served, monkeypatch):
        monkeypatch.setattr(st, "_http_get", lambda *a, **k: FakeResp(200, {"Content-Length": "3000000"}, b"z" * 3_000_000))
        m = svc.open_media(VID, "18", "bytes=0-99")
        assert m.headers["Content-Range"] == "bytes 0-99/3000000" and len(b"".join(m.body)) == 100

    def test_a_cdn_that_ignores_the_range_parameter_mid_stream_is_an_error(self, served, monkeypatch):
        monkeypatch.setattr(st, "_http_get", lambda *a, **k: FakeResp(200, {"Content-Length": "3000000"}, b"z" * 3_000_000))
        with pytest.raises(UpstreamError, match="ignored"):
            svc.open_media(VID, "18", "bytes=50-99")

    def test_connection_closes_even_if_the_client_stops_reading(self, served, monkeypatch):
        resp = FakeResp(206, {"Content-Range": "bytes 0-9/3000000", "Content-Length": "10"}, b"x" * 10)
        monkeypatch.setattr(st, "_http_get", lambda *a, **k: resp)
        m = svc.open_media(VID, "18", "bytes=0-9")
        it = iter(m.body)
        next(it)
        it.close()
        assert resp.closed

    def test_mid_chunk_drop_ends_quietly(self, served, monkeypatch):
        class Flaky(FakeResp):
            def iter_content(self, n):
                yield b"x"
                raise requests.ConnectionError("reset")

        monkeypatch.setattr(st, "_http_get", lambda *a, **k: Flaky(206, {"Content-Range": "bytes 0-9/3000000", "Content-Length": "10"}))
        assert b"".join(svc.open_media(VID, "18", "bytes=0-9").body) == b"x"


class TestCache:
    def test_info_is_cached(self, monkeypatch):
        svc._cache.clear()
        n = {"c": 0}
        monkeypatch.setattr(svc, "_extract", lambda vid: n.__setitem__("c", n["c"] + 1) or raw_video())
        svc.info(VID)
        svc.info(VID)
        assert n["c"] == 1
        svc._cache.clear()

    def test_ttl_expiry_reextracts(self, monkeypatch):
        svc._cache.clear()
        n = {"c": 0}
        monkeypatch.setattr(svc, "_extract", lambda vid: n.__setitem__("c", n["c"] + 1) or raw_video())
        svc.info(VID)
        ts, raw, info = svc._cache[VID]
        svc._cache[VID] = (ts - st._CACHE_TTL_S - 1, raw, info)
        svc.info(VID)
        assert n["c"] == 2
        svc._cache.clear()

    def test_bounded_lru(self, monkeypatch):
        svc._cache.clear()
        monkeypatch.setattr(svc, "_extract", lambda vid: raw_video())
        monkeypatch.setattr(st, "_CACHE_MAX", 3)
        ids = [f"{i:011d}" for i in range(5)]
        for i in ids:
            svc.info(i)
        assert list(svc._cache) == ids[-3:]
        svc._cache.clear()

    def test_extraction_failure_is_a_user_error(self, monkeypatch):
        from tests.fakes import make_download_error

        svc._cache.clear()

        def boom(vid):
            raise make_download_error("[youtube] x: Video unavailable")

        monkeypatch.setattr(svc, "_extract", boom)
        with pytest.raises(InvalidRequestError, match="unavailable"):
            svc.info(VID)
        assert VID not in svc._cache

    def test_invalid_id_never_extracts(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda vid: pytest.fail("extract called"))
        with pytest.raises(InvalidRequestError):
            svc.info("nope")


VTT = ("WEBVTT\nKind: captions\n\n00:00:01.000 --> 00:00:03.000 align:start position:0%\n"
       "never<00:00:01.500><c> gonna</c><00:00:02.000><c.colorE5E5E5> give</c> you up\n")


class TestSubtitles:
    @pytest.fixture
    def subs(self, monkeypatch):
        svc._cache.clear()
        raw = raw_video(subtitles={"en": [{"ext": "srv3", "url": "https://www.youtube.com/s.srv3"}, {"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?v=1&lang=en"}]},
                        automatic_captions={"en": [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?v=1&lang=en&kind=asr"}]})
        monkeypatch.setattr(svc, "_extract", lambda vid: raw)
        got = []

        def fake_get(url, timeout=20):
            got.append(url)
            return FakeResp(200, {}, VTT.encode())

        monkeypatch.setattr(st.requests, "get", fake_get)
        yield got
        svc._cache.clear()

    def test_manual_track_is_cleaned(self, subs):
        text = svc.subtitle_vtt(VID, "en")
        assert text.startswith("WEBVTT") and "<c" not in text and "<00:" not in text
        assert "never gonna give you up" in text and "kind=asr" not in subs[0]

    def test_auto_track_uses_the_asr_url(self, subs):
        svc.subtitle_vtt(VID, "en", auto=True)
        assert "kind=asr" in subs[0]

    def test_missing_language(self, subs):
        with pytest.raises(InvalidRequestError, match="No WebVTT"):
            svc.subtitle_vtt(VID, "xx")

    def test_non_vtt_response_is_an_upstream_error(self, subs, monkeypatch):
        monkeypatch.setattr(st.requests, "get", lambda *a, **k: FakeResp(200, {}, b"<html>captcha</html>"))
        with pytest.raises(UpstreamError):
            svc.subtitle_vtt(VID, "en")

    def test_foreign_host_refused(self, monkeypatch):
        svc._cache.clear()
        raw = raw_video(subtitles={"en": [{"ext": "vtt", "url": "http://evil.example/x.vtt"}]})
        monkeypatch.setattr(svc, "_extract", lambda vid: raw)
        monkeypatch.setattr(st.requests, "get", lambda *a, **k: pytest.fail("fetched a foreign host"))
        with pytest.raises(InvalidRequestError, match="non-YouTube"):
            svc.subtitle_vtt(VID, "en")
        svc._cache.clear()

    def test_clean_vtt(self):
        assert clean_vtt("a<00:00:01.200><c>b</c> <c.x>c</c>") == "ab c"


# ───────────────────────── HTTP layer ─────────────────────────


@pytest.fixture
def client(served):
    return TestClient(create_app())


class TestStreamApi:
    def test_info(self, client):
        r = client.get(f"/api/stream/{VID}/info")
        assert r.status_code == 200
        j = r.json()
        assert j["title"] == "Song" and [q["height"] for q in j["qualities"]][:2] == [2160, 1080] and j["audio_format"] == "140"

    def test_bad_id_is_422(self, client):
        assert client.get("/api/stream/short/info").status_code == 422

    def test_media_returns_a_seekable_206(self, client):
        r = client.get(f"/api/stream/{VID}/media", params={"format": "18"}, headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and r.headers["accept-ranges"] == "bytes"
        assert r.headers["content-range"].startswith("bytes 0-99/") and len(r.content) == 100 and r.headers["content-type"] == "video/mp4"

    def test_media_validation(self, client):
        assert client.get(f"/api/stream/{VID}/media").status_code == 422             # format required
        assert client.get(f"/api/stream/{VID}/media", params={"format": "zzz"}).status_code == 422
        assert client.get(f"/api/stream/{VID}/media", params={"format": "18"}, headers={"Range": "bytes=oops"}).status_code == 422

    def test_expired_stream_is_502(self, client, monkeypatch):
        monkeypatch.setattr(st, "_http_get", lambda *a, **k: FakeResp(403))
        r = client.get(f"/api/stream/{VID}/media", params={"format": "18"})
        assert r.status_code == 502 and "expired" in r.json()["detail"]

    def test_subs(self, client, monkeypatch):
        svc._cache.clear()
        raw = raw_video(subtitles={"en": [{"ext": "vtt", "url": "https://www.youtube.com/t?v=1"}]})
        monkeypatch.setattr(svc, "_extract", lambda vid: raw)
        monkeypatch.setattr(st.requests, "get", lambda *a, **k: FakeResp(200, {}, VTT.encode()))
        r = client.get(f"/api/stream/{VID}/subs", params={"lang": "en"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/vtt") and r.text.startswith("WEBVTT")
        assert client.get(f"/api/stream/{VID}/subs", params={"lang": "zz"}).status_code == 422
        assert client.get(f"/api/stream/{VID}/subs").status_code == 422


# ───────────────────────── library subtitles ─────────────────────────


@pytest.fixture
def sample_video(downloads_dir):
    """A 1-second mp4 with an embedded mov_text track and a sidecar .srt (made by the real ffmpeg)."""
    ff = get_ffmpeg_path()
    srt = downloads_dir / "src.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:00,900\nHello world\n", encoding="utf-8")
    out = downloads_dir / "Clip" / "clip.mp4"
    out.parent.mkdir()
    cmd = [ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=10", "-f", "lavfi", "-i", "sine=d=1",
           "-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "mpeg4", "-c:a", "aac", "-c:s", "mov_text",
           "-metadata:s:s:0", "language=eng", "-shortest", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"this ffmpeg build cannot make the sample: {r.stderr[-200:]}")
    srt.unlink()
    (out.parent / "clip.en.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nSide car\n", encoding="utf-8")
    (out.parent / "clip.hi.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:00.500\nनमस्ते\n", encoding="utf-8")
    (out.parent / "other.srt").write_text("1\n00:00:00,000 --> 00:00:00,500\nnot mine\n", encoding="utf-8")
    return "Clip/clip.mp4"


class TestLibrarySubtitles:
    def test_lists_embedded_and_sidecar_tracks(self, sample_video):
        tracks = {t.id: t for t in lib.subtitle_sources(sample_video)}
        emb = [t for t in tracks.values() if t.source == "embedded"]
        assert len(emb) == 1 and emb[0].lang == "eng" and emb[0].id.startswith("emb:")
        assert {"side:clip.en.srt", "side:clip.hi.vtt"} <= set(tracks) and "side:other.srt" not in tracks
        assert tracks["side:clip.en.srt"].lang == "en"

    def test_extracts_embedded_track_as_webvtt(self, sample_video):
        (emb,) = [t for t in lib.subtitle_sources(sample_video) if t.source == "embedded"]
        vtt = lib.subtitle_vtt(sample_video, emb.id)
        assert vtt.startswith("WEBVTT") and "Hello world" in vtt

    def test_sidecar_srt_is_converted(self, sample_video):
        vtt = lib.subtitle_vtt(sample_video, "side:clip.en.srt")
        assert vtt.startswith("WEBVTT") and "00:00:00.500" in vtt and "," not in vtt.split("-->")[1].split("\n")[0]

    def test_sidecar_vtt_passes_through(self, sample_video):
        assert "नमस्ते" in lib.subtitle_vtt(sample_video, "side:clip.hi.vtt")

    @pytest.mark.parametrize("track", ["side:../../etc/passwd", "side:..\\x.srt", "side:sub/x.srt", "side:other.mp3", "emb:abc", "emb:", "nonsense", ""])
    def test_bad_tracks(self, sample_video, track):
        with pytest.raises((InvalidRequestError, NotFoundError)):
            lib.subtitle_vtt(sample_video, track)

    def test_missing_embedded_stream(self, sample_video):
        with pytest.raises(NotFoundError):
            lib.subtitle_vtt(sample_video, "emb:9")

    def test_path_safety(self, sample_video):
        with pytest.raises(InvalidRequestError):
            lib.subtitle_sources("../x.mp4")
        with pytest.raises(NotFoundError):
            lib.subtitle_sources("Clip/ghost.mp4")

    def test_endpoints(self, sample_video, fake_ydl):
        c = TestClient(create_app())
        r = c.get("/api/library/subtitles", params={"path": sample_video})
        assert r.status_code == 200 and len(r.json()) == 3
        r = c.get("/api/library/subtitle", params={"path": sample_video, "track": "side:clip.hi.vtt"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/vtt")
        assert c.get("/api/library/subtitle", params={"path": sample_video, "track": "side:../x"}).status_code == 422
        assert c.get("/api/library/subtitles", params={"path": "../x"}).status_code == 422
