"""Lyrics: YouTube-title cleanup, LRC parsing, provider lookup + fallback, caching, the endpoint."""

from __future__ import annotations

import pytest
import requests
from fastapi.testclient import TestClient

from api.main import create_app
from configs import get_settings
from services import lyrics_service
from services.lyrics import clean_title, parse_lrc

LRC = "[00:01.00]<00:01.00>First <00:01.50>line\n[00:05.20]Second\n[ar:Someone]\n[00:09.00][00:20.00]Chorus"


class TestCleanTitle:
    @pytest.mark.parametrize("title, uploader, expected", [
        ("Rick Astley - Never Gonna Give You Up (Official Music Video)", "Rick Astley", ("Rick Astley", "Never Gonna Give You Up")),
        ("Imagine Dragons - Believer [4K] | Evolve", None, ("Imagine Dragons", "Believer")),
        ("Shape of You (feat. Someone) [Lyrics]", "Ed Sheeran - Topic", ("Ed Sheeran", "Shape of You")),
        ("Song ft. Guest", "ArtistVEVO", ("Artist", "Song")),
        ("Plain Title", None, ("", "Plain Title")),
    ])
    def test_cases(self, title, uploader, expected):
        assert clean_title(title, uploader) == expected


def test_parse_lrc_handles_word_stamps_and_repeats():
    lines = parse_lrc(LRC)
    assert [(line.t, line.text) for line in lines] == [
        (1.0, "First line"), (5.2, "Second"), (9.0, "Chorus"), (20.0, "Chorus")]
    assert parse_lrc(None) == [] and parse_lrc("no stamps") == []


@pytest.fixture
def provider(monkeypatch):
    """Scripted LRCLIB: records calls; ``replies`` maps path → JSON (or an exception)."""
    calls = []
    replies = {}

    def http(path, params):
        calls.append((path, params))
        r = replies.get(path)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(lyrics_service, "_http", http)
    return calls, replies


class TestFind:
    def test_exact_lookup_with_duration(self, provider):
        calls, replies = provider
        replies["/api/get"] = {"trackName": "Song", "artistName": "Artist", "syncedLyrics": LRC, "plainLyrics": "x"}
        res = lyrics_service.find("Artist - Song (Official Video)", None, 212.6)
        assert res.found and res.source == "LRCLIB" and len(res.synced) == 4
        assert calls == [("/api/get", {"artist_name": "Artist", "track_name": "Song", "duration": 213})]

    def test_search_fallback_prefers_synced_and_close_duration(self, provider):
        calls, replies = provider
        replies["/api/get"] = None
        replies["/api/search"] = [
            {"trackName": "Song (far)", "duration": 400, "syncedLyrics": LRC},
            {"trackName": "Song (plain)", "duration": 200, "plainLyrics": "words"},
            {"trackName": "Song (best)", "duration": 201, "syncedLyrics": LRC},
        ]
        res = lyrics_service.find("Artist - Song", None, 200)
        assert res.track == "Song (best)" and calls[-1][0] == "/api/search"

    def test_nothing_found(self, provider):
        _, replies = provider
        replies["/api/search"] = []
        res = lyrics_service.find("Unknown thing")
        assert not res.found and "No lyrics" in res.message

    def test_results_are_cached(self, provider):
        calls, replies = provider
        replies["/api/get"] = {"trackName": "S", "artistName": "A", "plainLyrics": "la"}
        lyrics_service.find("A - S", None, 100)
        lyrics_service.find("A - S", None, 100)
        assert len(calls) == 1

    def test_provider_down_is_a_message_not_an_error(self, provider):
        _, replies = provider
        replies["/api/get"] = requests.ConnectionError("down")
        res = lyrics_service.find("A - S")
        assert not res.found and "not reachable" in res.message

    def test_disabled(self, monkeypatch, provider):
        monkeypatch.setattr(get_settings().download, "lyrics_url", None)
        res = lyrics_service.find("A - S")
        assert not res.found and "turned off" in res.message and provider[0] == []


def test_endpoint(provider, downloads_dir):
    _, replies = provider
    replies["/api/get"] = {"trackName": "S", "artistName": "A", "syncedLyrics": LRC}
    r = TestClient(create_app()).get("/api/lyrics", params={"title": "A - S", "duration": 99})
    body = r.json()
    assert r.status_code == 200 and body["found"] and body["synced"][0] == {"t": 1.0, "text": "First line"}
    assert TestClient(create_app()).get("/api/lyrics").status_code == 422
