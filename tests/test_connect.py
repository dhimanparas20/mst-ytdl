"""Extension sign-in: one-time pairing code, ownership, switch, the zip served to the extension."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from configs import get_settings
from services.admin_config import KNOBS
from tests.test_server_mode import FUTURE, _netscape


def _logged_out() -> str:
    return "# Netscape HTTP Cookie File\n" + f".youtube.com\tTRUE\t/\tTRUE\t{FUTURE}\tPREF\tz\n"


@pytest.fixture
def personal(monkeypatch, tmp_path, downloads_dir):
    monkeypatch.setattr(get_settings().server, "mode", False)
    monkeypatch.setattr(get_settings().server, "connect_enabled", True)
    monkeypatch.setattr(get_settings().download, "cookies_file", tmp_path / "cookies.txt")
    monkeypatch.setattr(get_settings().download, "cookies_from_browser", None)
    return TestClient(create_app())


@pytest.fixture
def srv(monkeypatch, downloads_dir):
    s = get_settings().server
    for k, v in {"mode": True, "min_free_mb": 0, "rate_limit_per_minute": 1000, "connect_enabled": True}.items():
        monkeypatch.setattr(s, k, v)
    return create_app()


def browser(app) -> TestClient:
    c = TestClient(app)
    c.get("/api/config")  # receives the visitor id
    return c


class TestPersonalMode:
    def test_pair_and_link(self, personal):
        r = personal.post("/api/connect").json()
        code = r["code"]
        assert len(code) == 9 and code[4] == "-" and r["expires_in"] == 600
        assert personal.get(f"/api/connect/{code}").json() == {"state": "waiting"}
        assert not personal.get("/api/cookies").json()["loaded"]
        out = TestClient(personal.app).post(f"/api/connect/{code.lower()}", json={"text": _netscape()})  # the extension: no cookies, any case
        assert out.status_code == 200 and out.json()["ok"]
        assert personal.get("/api/cookies").json()["login"] is True
        assert personal.get(f"/api/connect/{code}").json()["state"] == "done"

    def test_code_is_single_use(self, personal):
        code = personal.post("/api/connect").json()["code"]
        assert personal.post(f"/api/connect/{code}", json={"text": _netscape()}).status_code == 200
        assert personal.post(f"/api/connect/{code}", json={"text": _netscape()}).status_code == 404

    def test_wrong_code(self, personal):
        assert personal.post("/api/connect/ABCD-EFGH", json={"text": _netscape()}).status_code == 404
        assert personal.get("/api/connect/ABCD-EFGH").status_code == 404

    def test_logged_out_cookies_are_refused_and_change_nothing(self, personal):
        assert personal.post(f"/api/connect/{personal.post('/api/connect').json()['code']}", json={"text": _netscape("keep-me")}).status_code == 200
        before = get_settings().download.cookies_path.read_text(encoding="utf-8")
        code = personal.post("/api/connect").json()["code"]
        r = personal.post(f"/api/connect/{code}", json={"text": _logged_out()})
        assert r.status_code == 422 and "not signed in" in r.json()["detail"]
        assert get_settings().download.cookies_path.read_text(encoding="utf-8") == before
        assert personal.get(f"/api/connect/{code}").json()["state"] == "waiting"  # can try again


class TestServerMode:
    def test_cookies_go_to_the_visitor_that_asked(self, srv):
        a, b = browser(srv), browser(srv)
        code = a.post("/api/connect").json()["code"]
        assert b.get(f"/api/connect/{code}").status_code == 404  # another visitor cannot even watch it
        assert TestClient(srv).post(f"/api/connect/{code}", json={"text": _netscape()}).status_code == 200
        assert a.get("/api/my-cookies").json()["login"] is True
        assert b.get("/api/my-cookies").json()["loaded"] is False
        assert a.get(f"/api/connect/{code}").json()["state"] == "done"


class TestSwitch:
    def test_off_refuses_everything_and_hides_the_button(self, srv, monkeypatch):
        c = browser(srv)
        code = c.post("/api/connect").json()["code"]
        monkeypatch.setattr(get_settings().server, "connect_enabled", False)
        assert c.get("/api/config").json()["connect_enabled"] is False
        assert c.post("/api/connect").status_code == 403
        assert TestClient(srv).post(f"/api/connect/{code}", json={"text": _netscape()}).status_code == 403
        assert c.put("/api/my-cookies", json={"text": _netscape()}).status_code == 200  # pasting always works

    def test_it_is_an_admin_knob(self):
        assert any(k.key == "server.connect_enabled" and k.kind == "bool" for k in KNOBS)


class TestExtensionZip:
    def test_zip_is_preset_to_this_server(self, personal):
        r = personal.get("/api/connect/extension.zip")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        assert set(z.namelist()) >= {"manifest.json", "popup.html", "popup.js", "config.json", "icon.png"}
        cfg = json.loads(z.read("config.json"))
        manifest = json.loads(z.read("manifest.json"))
        assert cfg["server"] == "http://testserver" and "http://testserver/*" in manifest["host_permissions"]
        assert manifest["manifest_version"] == 3 and "cookies" in manifest["permissions"]
