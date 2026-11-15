"""Account view: the signed-in user's playlists and home feed (yt-dlp stubbed)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from configs import get_settings
from services import metadata_service
from tests.test_server_mode import _netscape, server_cookie_file  # noqa: F401  (fixture re-used)

PLAYLISTS = [
    {"id": "PLabc", "title": "Gym", "channel": "Me", "thumbnails": [{"url": "http://t/1.jpg"}], "playlist_count": 12},
    {"id": "LL", "title": "Liked videos"},  # already pinned → dropped
    {"id": "PLabc", "title": "dupe"},
    {"id": "PLxyz", "title": "  "},  # untitled → dropped
]
HOME = [
    {"id": "dQw4w9WgXcQ", "title": "Song A", "channel": "Artist", "duration": 200, "ie_key": "Youtube"},
    {"id": "PLsomething", "title": "A mix", "ie_key": "YoutubeTab"},  # not a video
    {"id": "9bZkp7q19f0", "title": "Song B", "duration": 100},
    {"id": "aaaaaaaaaaa", "title": "[Private video]"},
]


@pytest.fixture
def feeds(monkeypatch):
    calls: list[str] = []

    def fake(url, limit):
        calls.append(url)
        return PLAYLISTS if "playlists" in url else HOME

    monkeypatch.setattr(metadata_service, "feed", fake)
    return calls


@pytest.fixture
def personal(monkeypatch, server_cookie_file, downloads_dir):  # noqa: F811
    monkeypatch.setattr(get_settings().server, "mode", False)
    return TestClient(create_app())


class TestPersonalMode:
    def test_playlists_pinned_first_then_library(self, personal, feeds):
        r = personal.get("/api/account/playlists").json()
        assert r["signed_in"] and r["message"] is None
        assert [p["id"] for p in r["playlists"]] == ["LM", "LL", "WL", "PLabc"]
        assert r["playlists"][0]["music"] and r["playlists"][0]["url"].startswith("https://music.youtube.com/")
        last = r["playlists"][-1]
        assert last["url"] == "https://www.youtube.com/playlist?list=PLabc" and last["count"] == 12 and last["uploader"] == "Me"

    def test_home_keeps_only_playable_videos(self, personal, feeds):
        r = personal.get("/api/account/home").json()
        assert [e["video_id"] for e in r["entries"]] == ["dQw4w9WgXcQ", "9bZkp7q19f0"]
        assert r["entries"][0]["url"].endswith("watch?v=dQw4w9WgXcQ") and r["entries"][0]["uploader"] == "Artist"

    def test_without_login_cookies(self, personal, feeds, monkeypatch, tmp_path):
        monkeypatch.setattr(get_settings().download, "cookies_file", tmp_path / "missing.txt")
        assert personal.get("/api/account/playlists").json() == {"signed_in": False, "playlists": [],
                                                                  "message": "Add your YouTube cookies to see your playlists and home feed."}
        assert not personal.get("/api/account/home").json()["signed_in"]
        assert feeds == []  # nothing was asked of YouTube

    def test_expired_cookies_give_a_message_not_an_error(self, personal, monkeypatch):
        def boom(url, limit):
            raise RuntimeError("HTTP Error 401: Unauthorized")

        monkeypatch.setattr(metadata_service, "feed", boom)
        pl = personal.get("/api/account/playlists")
        assert pl.status_code == 200 and pl.json()["message"] and len(pl.json()["playlists"]) == 3  # pinned still listed
        home = personal.get("/api/account/home")
        assert home.status_code == 200 and home.json()["entries"] == [] and home.json()["message"]


class TestServerMode:
    @pytest.fixture
    def srv(self, monkeypatch, server_cookie_file, downloads_dir):  # noqa: F811
        s = get_settings().server
        for k, v in {"mode": True, "min_free_mb": 0, "rate_limit_per_minute": 1000, "share_server_cookies": True,
                     "cache_minutes": 10}.items():
            monkeypatch.setattr(s, k, v)
        return create_app()

    def test_server_cookies_are_never_used_even_when_shared(self, srv, feeds):
        c = TestClient(srv)
        c.get("/api/config")
        assert c.get("/api/account/playlists").json()["signed_in"] is False
        assert c.get("/api/account/home").json()["signed_in"] is False
        assert feeds == []

    def test_visitor_with_own_cookies_and_per_visitor_cache(self, srv, feeds):
        a, b = TestClient(srv), TestClient(srv)
        for c in (a, b):
            c.get("/api/config")
            assert c.put("/api/my-cookies", json={"text": _netscape()}).status_code == 200
        assert a.get("/api/account/playlists").json()["signed_in"]
        a.get("/api/account/playlists")
        assert len(feeds) == 1  # second read came from the cache
        b.get("/api/account/playlists")
        assert len(feeds) == 2  # another visitor never shares it
