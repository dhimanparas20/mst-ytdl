"""services/metadata.py — URL decisions, playlist expansion, previews (yt-dlp stubbed)."""

from __future__ import annotations

import re

import pytest

from models import JobKind
from models import PlaylistMode as PM
from models.paging import SEARCH_POOL_CAP
from modules.ytdlp_util import UrlError, parse_url
from services import InvalidRequestError
from services import metadata_service as svc
from services.metadata import classify
from tests.fakes import make_download_error

VID = "dQw4w9WgXcQ"
PL = "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"
WATCH = f"https://www.youtube.com/watch?v={VID}"
BOTH = f"https://www.youtube.com/watch?v={VID}&list={PL}"
PLAYLIST = f"https://www.youtube.com/playlist?list={PL}"


class TestClassify:
    @pytest.mark.parametrize(
        "url,mode,kind",
        [
            (WATCH, PM.AUTO, JobKind.SINGLE),
            (WATCH, PM.SINGLE, JobKind.SINGLE),
            (BOTH, PM.AUTO, JobKind.SINGLE),
            (BOTH, PM.SINGLE, JobKind.SINGLE),
            (BOTH, PM.PLAYLIST, JobKind.PLAYLIST),
            (PLAYLIST, PM.AUTO, JobKind.PLAYLIST),
            (PLAYLIST, PM.PLAYLIST, JobKind.PLAYLIST),
            ("https://www.youtube.com/@chan", PM.AUTO, JobKind.PLAYLIST),
            ("https://music.youtube.com/browse/MPREb_abcdefgh", PM.AUTO, JobKind.PLAYLIST),
        ],
    )
    def test_matrix(self, url, mode, kind):
        assert classify(parse_url(url), mode) is kind

    def test_forcing_playlist_on_plain_video_errors(self):
        with pytest.raises(UrlError, match="no playlist"):
            classify(parse_url(WATCH), PM.PLAYLIST)

    def test_forcing_single_on_pure_playlist_errors(self):
        with pytest.raises(UrlError, match="there is no single video"):
            classify(parse_url(PLAYLIST), PM.SINGLE)


class TestResolveTarget:
    def test_single_target_is_canonical(self):
        t = svc.resolve_target(f"{WATCH}&t=5&si=abc")
        assert (t.kind, t.url) == (JobKind.SINGLE, WATCH)

    def test_ambiguous_default_single_but_flagged(self):
        t = svc.resolve_target(BOTH)
        assert t.kind is JobKind.SINGLE and t.parsed.ambiguous and t.url == WATCH

    def test_ambiguous_forced_playlist(self):
        t = svc.resolve_target(BOTH, PM.PLAYLIST)
        assert t.kind is JobKind.PLAYLIST and t.url == PLAYLIST

    def test_music_stays_music(self):
        t = svc.resolve_target(f"https://music.youtube.com/watch?v={VID}")
        assert t.url == f"https://music.youtube.com/watch?v={VID}" and t.parsed.is_music

    @pytest.mark.parametrize("bad", ["https://vimeo.com/1", "not a url at all", "", "https://youtube.com/watch?v=x"])
    def test_bad_urls_become_user_facing_errors(self, bad):
        with pytest.raises(InvalidRequestError):
            svc.resolve_target(bad)

    def test_allow_list_is_enforced(self, monkeypatch):
        from configs import get_settings

        monkeypatch.setattr(get_settings().download, "allowed_hosts", ["youtube.com"])
        svc.resolve_target(WATCH)
        with pytest.raises(InvalidRequestError, match="not allowed"):
            svc.resolve_target(f"https://music.youtube.com/watch?v={VID}")


class TestFolders:
    def test_single_folder_is_todays_date(self):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", svc.folder_for_single())

    def test_single_folder_respects_timezone_and_format(self, monkeypatch):
        from configs import get_settings

        dl = get_settings().download
        monkeypatch.setattr(dl, "date_folder_format", "%d.%m.%Y")
        assert re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", svc.folder_for_single())

    def test_override_wins_and_is_sanitised(self):
        assert svc.folder_for_single("My/Stuff?") == "My/Stuff"  # nested folders are allowed
        assert svc.folder_for_playlist("Original", "Custom: Name") == "Custom Name"

    def test_nested_override_for_playlist_and_traversal_is_neutralised(self):
        assert svc.folder_for_playlist("Orig", "Music/Rock") == "Music/Rock"
        assert svc.folder_for_single("../../etc/passwd") == "etc/passwd"
        assert svc.folder_for_single("../..") == "untitled"
        assert svc.folder_for_single("a/b/c/d/e/f") == "a/b/c/d"  # depth is capped

    def test_title_slash_is_text_not_nesting(self):
        assert svc.folder_for_playlist("AC/DC Hits") == "ACDC Hits"

    def test_playlist_folder_is_sanitised_title(self):
        assert svc.folder_for_playlist('Best of "90s" / Rock') == "Best of 90s  Rock".replace("  ", " ")
        assert svc.folder_for_playlist("") == "playlist"
        assert svc.folder_for_playlist("../../etc") == "etc"


def flat_info(entries):
    return {"title": " My Mix ", "uploader": "Someone", "thumbnails": [{"url": "http://t/small"}, {"url": "http://t/big"}],
            "entries": entries}


class TestExpandPlaylist:
    def test_entries_mapped(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: flat_info([
            {"id": "a" * 11, "title": "One", "duration": 61, "thumbnails": [{"url": "http://x/1"}, {"url": "http://x/2"}]},
            {"id": "b" * 11, "title": "Two"},
        ]))
        pl = svc.expand_playlist(PLAYLIST)
        assert (pl.title, pl.uploader, pl.thumbnail) == ("My Mix", "Someone", "http://t/big")
        e1, e2 = pl.entries
        assert (e1.index, e1.title, e1.duration, e1.thumbnail, e1.available) == (1, "One", 61, "http://x/2", True)
        assert e1.url == f"https://www.youtube.com/watch?v={'a' * 11}"
        assert e2.thumbnail == f"https://i.ytimg.com/vi/{'b' * 11}/mqdefault.jpg"  # synthesised

    def test_private_deleted_and_none_entries_flagged(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: flat_info([
            {"id": "a" * 11, "title": "OK"},
            {"id": "b" * 11, "title": "[Private video]"},
            {"id": "c" * 11, "title": "[Deleted video]"},
            None,
            {"title": "no id"},
        ]))
        ok = [e.available for e in svc.expand_playlist(PLAYLIST).entries]
        assert ok == [True, False, False, False, False]

    def test_indices_are_original_positions(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: flat_info([None, {"id": "a" * 11, "title": "X"}]))
        assert [e.index for e in svc.expand_playlist(PLAYLIST).entries] == [1, 2]

    def test_music_playlist_yields_music_urls(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: flat_info([{"id": "a" * 11, "title": "X"}]))
        pl = svc.expand_playlist("https://music.youtube.com/playlist?list=OLAK5uy_x")
        assert pl.entries[0].url.startswith("https://music.youtube.com/")

    def test_empty_and_missing_metadata_defaults(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: {"entries": []})
        pl = svc.expand_playlist(PLAYLIST)
        assert pl.title == "Playlist" and pl.entries == [] and pl.thumbnail is None

    def test_untitled_entry(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: flat_info([{"id": "a" * 11, "title": "  "}]))
        assert svc.expand_playlist(PLAYLIST).entries[0].title == "[untitled]"

    def test_flat_flag_is_requested(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(svc, "_extract", lambda url, flat: seen.update(flat=flat) or flat_info([]))
        svc.expand_playlist(PLAYLIST)
        assert seen["flat"] is True


def video_info(**kw):
    base = {"id": VID, "title": " Never Gonna ", "uploader": "Rick", "duration": 212, "thumbnail": "http://t/x.jpg",
            "formats": [
                {"height": 360, "vcodec": "avc1"}, {"height": 720, "vcodec": "avc1"}, {"height": 1080, "vcodec": "vp9"},
                {"height": 720, "vcodec": "vp9"},  # duplicate height
                {"height": None, "vcodec": "none"},  # audio only
                {"height": 1440, "vcodec": "none"},  # not a video stream
                {"vcodec": "avc1"},  # no height
            ]}
    return {**base, **kw}


class TestPreview:
    def test_single(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: video_info())
        m = svc.preview(WATCH)
        assert m.kind is JobKind.SINGLE and m.title == "Never Gonna" and m.duration == 212
        assert m.available_heights == [360, 720, 1080]  # sorted, unique, video-only
        assert m.entry_count == 1 and not m.ambiguous
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", m.folder_preview)

    def test_single_falls_back_to_synthetic_thumbnail(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: video_info(thumbnail=None))
        assert svc.preview(WATCH).thumbnail == f"https://i.ytimg.com/vi/{VID}/mqdefault.jpg"

    def test_ambiguous_flag_surfaces(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: video_info())
        assert svc.preview(BOTH).ambiguous is True

    def test_playlist_preview(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: flat_info([{"id": "a" * 11, "title": "A"}, {"id": "b" * 11, "title": "B"}]))
        m = svc.preview(PLAYLIST)
        assert m.kind is JobKind.PLAYLIST and m.entry_count == 2 and m.folder_preview == "My Mix"
        assert m.available_heights == [] and len(m.entries) == 2

    def test_ambiguous_url_forced_to_playlist(self, monkeypatch):
        calls = []
        monkeypatch.setattr(svc, "_extract", lambda url, flat: calls.append((url, flat)) or flat_info([]))
        m = svc.preview(BOTH, PM.PLAYLIST)
        assert m.kind is JobKind.PLAYLIST and calls == [(PLAYLIST, True)] and m.ambiguous

    def test_yt_dlp_failure_becomes_user_error(self, monkeypatch):
        def boom(url, flat):
            raise make_download_error("[youtube] x: Video unavailable")

        monkeypatch.setattr(svc, "_extract", boom)
        with pytest.raises(InvalidRequestError, match="Video unavailable"):
            svc.preview(WATCH)

    def test_bad_url_never_reaches_network(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda *a: pytest.fail("network call for a bad URL"))
        with pytest.raises(InvalidRequestError):
            svc.preview("https://vimeo.com/1")


class TestSearch:
    RAW = {"entries": [
        {"id": "a" * 11, "title": " Song A ", "channel": "Chan", "duration": 200,
         "thumbnails": [{"url": "http://t/1"}, {"url": "http://t/2"}]},
        {"id": "UC1234567890abcdefghijkl", "title": "A channel result"},  # not a video id
        {"id": "short", "title": "junk"},
        None,
        {"title": "no id"},
        {"id": "b" * 11, "title": "", "uploader": "Up"},
    ]}

    def test_maps_only_videos_and_renumbers(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(svc, "_extract", lambda url, flat: seen.update(url=url, flat=flat) or self.RAW)
        res = svc.search("lofi beats", 5)
        assert seen == {"url": "ytsearch5:lofi beats", "flat": True}
        assert [r.index for r in res] == [1, 2] and [r.video_id for r in res] == ["a" * 11, "b" * 11]
        assert res[0].title == "Song A" and res[0].uploader == "Chan" and res[0].duration == 200
        assert res[0].thumbnail == "http://t/2" and res[0].url == f"https://www.youtube.com/watch?v={'a' * 11}"
        assert res[1].title == "[untitled]" and res[1].uploader == "Up"
        assert res[1].thumbnail == f"https://i.ytimg.com/vi/{'b' * 11}/mqdefault.jpg"

    def test_music_flag_switches_host(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: self.RAW)
        assert svc.search("x y", music=True)[0].url.startswith("https://music.youtube.com/watch?v=")

    @pytest.mark.parametrize("limit,asked", [(0, 1), (-5, 1), (10, 10), (999, SEARCH_POOL_CAP)])
    def test_limit_is_clamped(self, monkeypatch, limit, asked):
        seen = {}
        monkeypatch.setattr(svc, "_extract", lambda url, flat: seen.update(url=url) or {"entries": []})
        svc.search("query", limit)
        assert seen["url"] == f"ytsearch{asked}:query"

    @pytest.mark.parametrize("q", ["", " ", "a", "x" * 201, None])
    def test_bad_queries_rejected_before_network(self, monkeypatch, q):
        monkeypatch.setattr(svc, "_extract", lambda *a: pytest.fail("network call"))
        with pytest.raises(InvalidRequestError):
            svc.search(q)

    def test_query_is_trimmed(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(svc, "_extract", lambda url, flat: seen.update(url=url) or {"entries": []})
        svc.search("  hello world  ")
        assert seen["url"] == "ytsearch10:hello world"

    def test_yt_dlp_failure_is_user_error(self, monkeypatch):
        def boom(url, flat):
            raise make_download_error("HTTP Error 429: Too Many Requests")

        monkeypatch.setattr(svc, "_extract", boom)
        with pytest.raises(InvalidRequestError, match="429"):
            svc.search("anything")

    def test_empty_results(self, monkeypatch):
        monkeypatch.setattr(svc, "_extract", lambda url, flat: {"entries": []})
        assert svc.search("zzzz") == []


class TestExtractWiring:
    def test_empty_info_is_an_error(self, monkeypatch):
        import services.metadata as md

        class FakeYDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=False): return None

        monkeypatch.setattr(md, "YoutubeDL", FakeYDL)
        with pytest.raises(InvalidRequestError, match="no information"):
            svc._extract(WATCH, flat=False)

    def test_flat_caps_playlist_size(self, monkeypatch):
        import services.metadata as md
        from configs import get_settings

        monkeypatch.setattr(get_settings().download, "max_playlist_items", 7)
        seen = {}

        class FakeYDL:
            def __init__(self, opts): seen.update(opts)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=False): return {"entries": []}

        monkeypatch.setattr(md, "YoutubeDL", FakeYDL)
        svc._extract(PLAYLIST, flat=True)
        assert seen["playlistend"] == 7 and seen["extract_flat"] == "in_playlist"
