"""Home explore shelf: signed-in For you; anonymous Music/Videos via search."""

from __future__ import annotations

from models import AccountFeed, EntryInfo
from services import explore_service, metadata_service
from services.account import account_service
from services.cache import cache_service
from services.cookies import cookie_service


def _entry(i: int, *, music: bool = True) -> EntryInfo:
    vid = f"{i:011d}"[:11]
    return EntryInfo(
        index=i,
        video_id=vid,
        url=("https://music.youtube.com/watch?v=" if music else "https://www.youtube.com/watch?v=") + vid,
        title=f"Track {i}",
        uploader="Artist",
        duration=180,
        available=True,
    )


class _nullctx:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class TestExplore:
    def setup_method(self):
        cache_service.clear_json()

    def test_auto_anonymous_is_trending_music(self, monkeypatch):
        monkeypatch.setattr(account_service, "signed_in", lambda owner: False)
        monkeypatch.setattr(cookie_service, "scope", lambda owner: _nullctx())
        monkeypatch.setattr(
            metadata_service,
            "search",
            lambda q, limit=10, music=False: [_entry(abs(hash(q)) % 80 + n + 1, music=music) for n in range(min(limit, 3))],
        )
        feed = explore_service.get(None, "auto", 10)
        assert feed.source == "trending_music" and feed.entries and not feed.signed_in

    def test_auto_signed_in_is_for_you(self, monkeypatch):
        monkeypatch.setattr(account_service, "signed_in", lambda owner: True)
        monkeypatch.setattr(
            account_service,
            "home",
            lambda owner, limit=40: AccountFeed(signed_in=True, entries=[_entry(1, music=False)]),
        )
        monkeypatch.setattr(cookie_service, "scope", lambda owner: _nullctx())
        feed = explore_service.get("v1", "auto", 10)
        assert feed.source == "for_you" and feed.signed_in and feed.entries[0].title == "Track 1"

    def test_for_you_without_login_explains(self, monkeypatch):
        monkeypatch.setattr(account_service, "signed_in", lambda owner: False)
        monkeypatch.setattr(cookie_service, "scope", lambda owner: _nullctx())
        feed = explore_service.get(None, "for_you", 10)
        assert feed.source == "for_you" and not feed.entries and feed.message

    def test_videos_anonymous(self, monkeypatch):
        monkeypatch.setattr(account_service, "signed_in", lambda owner: False)
        monkeypatch.setattr(cookie_service, "scope", lambda owner: _nullctx())
        monkeypatch.setattr(
            metadata_service,
            "search",
            lambda q, limit=10, music=False: [_entry(n + 1, music=False) for n in range(min(limit, 4))],
        )
        feed = explore_service.get(None, "videos", 8)
        assert feed.source == "trending_videos" and not feed.signed_in
        assert all("youtube.com/watch" in e.url for e in feed.entries)

    def test_http_explore_anonymous_music(self, monkeypatch):
        from fastapi.testclient import TestClient

        from api.main import create_app

        monkeypatch.setattr(account_service, "signed_in", lambda owner: False)
        monkeypatch.setattr(cookie_service, "scope", lambda owner: _nullctx())
        monkeypatch.setattr(
            metadata_service,
            "search",
            lambda q, limit=10, music=False: [_entry(3, music=music)],
        )
        r = TestClient(create_app()).get("/api/explore", params={"kind": "music", "limit": 6, "page": 1})
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "trending_music" and body["signed_in"] is False and body["entries"]
        assert body["page"] == 1 and body["page_size"] == 6 and body["pages"] >= 1

    def test_trending_pagination(self, monkeypatch):
        monkeypatch.setattr(account_service, "signed_in", lambda owner: False)
        monkeypatch.setattr(cookie_service, "scope", lambda owner: _nullctx())

        def many(q, limit=10, music=False):
            return [_entry(n + 1 + abs(hash(q)) % 20, music=music) for n in range(limit)]

        monkeypatch.setattr(metadata_service, "search", many)
        from services.cache import cache_service
        cache_service.clear_json()
        p1 = explore_service.get(None, "music", 5, page=1)
        p2 = explore_service.get(None, "music", 5, page=2)
        assert p1.page == 1 and p2.page == 2
        assert p1.pages >= 2
        assert p1.entries and p2.entries
        assert p1.entries[0].video_id != p2.entries[0].video_id

