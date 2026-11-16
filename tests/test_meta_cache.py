"""JSON meta cache (previews / searches / playlists) — works in personal mode too, off at 0 minutes."""

from __future__ import annotations

import pytest

from configs import get_settings
from services import cache_service
from services import metadata_service as svc

VID = "dQw4w9WgXcQ"
WATCH = f"https://www.youtube.com/watch?v={VID}"
RAW = {"id": VID, "title": "Song", "uploader": "Rick", "duration": 10, "formats": []}


@pytest.fixture
def extract(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(svc, "_extract", lambda url, flat: calls.append(url) or dict(RAW))
    return calls


def test_personal_mode_caches_previews(extract):
    assert get_settings().server.mode is False
    first = svc.preview(WATCH)
    second = svc.preview(WATCH)
    assert len(extract) == 1 and second.title == first.title == "Song"


def test_personal_mode_caches_searches(monkeypatch):
    calls = []
    monkeypatch.setattr(svc, "_extract", lambda url, flat: calls.append(url) or {"entries": []})
    svc.search("lofi", 5)
    svc.search("LOFI", 5)  # case-insensitive key
    assert len(calls) == 1


def test_zero_minutes_turns_it_off(extract, monkeypatch):
    monkeypatch.setattr(get_settings().server, "cache_minutes", 0)
    svc.preview(WATCH)
    svc.preview(WATCH)
    assert len(extract) == 2


def test_ttl_follows_the_setting(extract, monkeypatch, fake_redis):
    monkeypatch.setattr(get_settings().server, "cache_minutes", 3)
    svc.preview(WATCH)
    (key,) = fake_redis.keys("*:META:*")
    assert 0 < fake_redis.ttl(key) <= 180


def test_clear_json_drops_entries(extract, fake_redis):
    svc.preview(WATCH)
    assert cache_service.clear_json() == 1
    svc.preview(WATCH)
    assert len(extract) == 2


def test_media_cache_stays_server_mode_only(tmp_path):
    from models import DownloadOptions, MediaFormat

    assert cache_service.enabled is False and cache_service.json_enabled is True
    assert cache_service.lookup_media(VID, DownloadOptions(format=MediaFormat.MP3)) is None

