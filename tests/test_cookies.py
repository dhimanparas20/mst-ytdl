"""Cookies: Netscape parsing, the service (validate / store / status) and the /api/cookies endpoints."""

from __future__ import annotations

import stat
import sys
import time

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from configs import get_settings
from modules.cookies_util import domain_matches, parse_netscape, render_netscape
from services import cookie_service
from services.errors import InvalidRequestError
from services.ytdl_opts import base_extract_opts

FUTURE = int(time.time()) + 86_400 * 200
PAST = int(time.time()) - 100


def _line(name="SID", domain=".youtube.com", exp=FUTURE, value="v", http_only=False):
    return f"{'#HttpOnly_' if http_only else ''}{domain}\tTRUE\t/\tTRUE\t{exp}\t{name}\t{value}"


GOOD = "\n".join(
    [
        "# Netscape HTTP Cookie File",
        _line("SID"),
        _line("__Secure-3PSID", http_only=True),
        _line("LOGIN_INFO"),
        _line("PREF"),
    ]
)


@pytest.fixture
def cookie_path(tmp_path, monkeypatch):
    path = tmp_path / "ck" / "cookies.txt"
    monkeypatch.setattr(get_settings().download, "cookies_file", path)
    monkeypatch.setattr(get_settings().download, "cookies_from_browser", None)
    return path


@pytest.fixture
def client(cookie_path):
    with TestClient(create_app()) as c:
        yield c


# ───────────── parsing ─────────────
def test_parse_basic_and_httponly():
    cookies, bad = parse_netscape(GOOD)
    assert bad == 0 and [c.name for c in cookies] == ["SID", "__Secure-3PSID", "LOGIN_INFO", "PREF"]
    assert cookies[1].http_only and not cookies[0].http_only


def test_parse_tolerates_crlf_and_space_separated_columns():
    text = ".youtube.com TRUE / TRUE 1893456000 SID abc\r\n\r\n# comment\r\n"
    cookies, bad = parse_netscape(text)
    assert bad == 0 and cookies[0].name == "SID" and cookies[0].value == "abc"


def test_parse_counts_bad_lines_and_roundtrips():
    cookies, bad = parse_netscape(GOOD + "\nnot a cookie line\n.x.com\tMAYBE\t/\tTRUE\t1\tn\tv")
    assert bad == 2 and len(cookies) == 4
    again, bad2 = parse_netscape(render_netscape(cookies))
    assert bad2 == 0 and again == cookies


def test_domain_matches():
    assert domain_matches(".youtube.com", ("youtube.com",))
    assert domain_matches("music.youtube.com", ("youtube.com",))
    assert not domain_matches("evilyoutube.com", ("youtube.com",))


# ───────────── service ─────────────
def test_status_without_file(cookie_path):
    st = cookie_service.status()
    assert st.loaded is False and st.source == "none" and st.count == 0


def test_save_then_status(cookie_path):
    st = cookie_service.save(GOOD)
    assert st.loaded and st.source == "file" and st.count == 4 and st.login
    assert st.expires_at and not st.warnings and st.expired == 0
    assert cookie_path.read_text().startswith("# Netscape HTTP Cookie File\n")
    assert "\r" not in cookie_path.read_text()
    assert not list(cookie_path.parent.glob(".cookies-*"))  # no temp leftovers


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_saved_file_is_owner_only(cookie_path):
    cookie_service.save(GOOD)
    assert stat.S_IMODE(cookie_path.stat().st_mode) == 0o600


def test_foreign_domains_are_dropped(cookie_path):
    st = cookie_service.save(GOOD + "\n" + _line("session", domain=".bank.example"))
    assert st.count == 4 and any("Ignored 1" in w for w in st.warnings)
    assert "bank.example" not in cookie_path.read_text()


def test_logged_out_export_warns(cookie_path):
    st = cookie_service.save(_line("PREF") + "\n" + _line("YSC"))
    assert st.loaded and not st.login and any("logged-out" in w for w in st.warnings)


def test_expired_login_warns(cookie_path):
    st = cookie_service.save(_line("SID", exp=PAST) + "\n" + _line("PREF"))
    assert st.expired == 1 and any("expired" in w for w in st.warnings)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("", "Paste"),
        ("   \n ", "Paste"),
        ('[{"name":"SID","value":"x"}]', "JSON"),
        ("hello world", "No cookies found"),
        (_line("x", domain=".other.example"), "youtube.com or google.com"),
    ],
)
def test_invalid_input_is_rejected_and_nothing_is_written(cookie_path, text, needle):
    with pytest.raises(InvalidRequestError, match=needle):
        cookie_service.save(text)
    assert not cookie_path.exists()


def test_too_large_is_rejected(cookie_path):
    with pytest.raises(InvalidRequestError, match="too large"):
        cookie_service.save(_line("SID", value="x" * 600_000))


def test_clear_removes_file_and_is_idempotent(cookie_path):
    cookie_service.save(GOOD)
    assert cookie_service.clear().loaded is False and not cookie_path.exists()
    assert cookie_service.clear().loaded is False


def test_browser_source_counts_as_loaded(cookie_path, monkeypatch):
    monkeypatch.setattr(get_settings().download, "cookies_from_browser", "chrome")
    st = cookie_service.status()
    assert st.loaded and st.source == "browser" and st.browser == "chrome"


def test_ytdlp_options_use_the_file_only_when_present(cookie_path):
    assert "cookiefile" not in base_extract_opts()
    cookie_service.save(GOOD)
    assert base_extract_opts()["cookiefile"] == str(cookie_path)


# ───────────── API ─────────────
def test_api_roundtrip_never_leaks_values(client, cookie_path):
    assert client.get("/api/cookies").json()["loaded"] is False
    r = client.put("/api/cookies", json={"text": GOOD.replace("\tv", "\tSUPERSECRET")})
    assert r.status_code == 200
    body = r.json()
    assert body["loaded"] and body["login"] and body["count"] == 4
    assert "SUPERSECRET" not in r.text and "SUPERSECRET" not in client.get("/api/cookies").text
    assert client.delete("/api/cookies").json()["loaded"] is False


def test_api_validation_errors_are_422(client):
    r = client.put("/api/cookies", json={"text": "nope"})
    assert r.status_code == 422 and "No cookies found" in r.json()["detail"]
    assert client.put("/api/cookies", json={}).status_code == 422


def test_saving_clears_the_stream_cache(client, monkeypatch):
    from services import stream_service

    cleared = []
    monkeypatch.setattr(stream_service, "clear", lambda: cleared.append(1))
    client.put("/api/cookies", json={"text": GOOD})
    client.delete("/api/cookies")
    assert len(cleared) == 2


# ───────────── live test (yt-dlp scripted) ─────────────
class _OkYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return {"formats": [{"vcodec": "avc1", "acodec": "none"}]}


class _BotYDL(_OkYDL):
    def extract_info(self, url, download=False):
        raise RuntimeError("Sign in to confirm you are not a bot")


def test_test_ok(cookie_path, monkeypatch):
    import services.cookies as ck

    monkeypatch.setattr(ck, "YoutubeDL", _OkYDL)
    cookie_service.save(GOOD)
    r = cookie_service.test()
    assert r.ok and "accepted" in r.message


def test_test_reports_bot_block(cookie_path, monkeypatch):
    import services.cookies as ck

    monkeypatch.setattr(ck, "YoutubeDL", _BotYDL)
    r = cookie_service.test()
    assert not r.ok and r.message


def test_test_flags_logged_out_cookies(cookie_path, monkeypatch):
    import services.cookies as ck

    monkeypatch.setattr(ck, "YoutubeDL", _OkYDL)
    cookie_service.save(_line("PREF"))
    assert "not a signed-in session" in cookie_service.test().message


def test_expiring_soon_warns(cookie_path):
    soon = int(time.time()) + 3 * 86_400
    st = cookie_service.save(_line("SID", exp=soon))
    assert any("within a week" in w for w in st.warnings)


def test_api_test_endpoint(client, monkeypatch):
    import services.cookies as ck

    monkeypatch.setattr(ck, "YoutubeDL", _OkYDL)
    r = client.post("/api/cookies/test")
    assert r.status_code == 200 and r.json()["ok"] is True
