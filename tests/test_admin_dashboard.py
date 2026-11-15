"""The /admin dashboard API: access, overview + analytics, activity, bans, file manager, maintenance, logs."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

import services.downloader as dl_mod
from api.main import create_app
from configs import get_settings
from modules.passwords import hash_password
from services import analytics, metadata_service
from services.access import VISITOR_COOKIE
from tests.fakes import FakeYDL

PW = "dash-board-pw"
_HASH = hash_password(PW)
WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture
def server(monkeypatch, downloads_dir, fake_ydl):
    s = get_settings().server
    for k, v in {"mode": True, "admin_password_hash": _HASH, "admin_username": "boss", "max_active_jobs": 50,
                 "min_free_mb": 0, "rate_limit_per_minute": 1000, "maintenance": False}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(dl_mod, "_CHECK_INTERVAL", 0.0)
    return s


@pytest.fixture
def app(server):
    return create_app()


def visitor(app) -> TestClient:
    c = TestClient(app)
    c.get("/api/config")
    return c


def admin(app) -> TestClient:
    c = TestClient(app)
    r = c.post("/api/admin/login", json={"username": "boss", "password": PW})
    assert r.status_code == 200, r.text
    return c


class TestAccess:
    def test_page_is_served(self, app):
        r = TestClient(app).get("/admin")
        assert r.status_code == 200 and "admin.js" in r.text and r.headers["x-robots-tag"] == "noindex"

    def test_username_and_password_are_both_checked(self, app):
        c = TestClient(app)
        assert c.post("/api/admin/login", json={"username": "admin", "password": PW}).status_code == 403
        assert c.post("/api/admin/login", json={"username": "boss", "password": "nope"}).status_code == 403
        st = c.post("/api/admin/login", json={"username": "BOSS", "password": PW}).json()  # username is case-insensitive
        assert st["admin"] and st["username"] == "boss"

    @pytest.mark.parametrize("path", ["/api/admin/overview", "/api/admin/jobs", "/api/admin/visitors", "/api/admin/files",
                                      "/api/admin/config", "/api/admin/maintenance", "/api/admin/logs"])
    def test_everything_needs_a_session(self, app, path):
        assert TestClient(app).get(path).status_code == 403
        assert admin(app).get(path).status_code == 200

    def test_personal_mode_without_password_is_open(self, downloads_dir, monkeypatch):
        monkeypatch.setattr(get_settings().server, "admin_password_hash", None)
        c = TestClient(create_app())
        assert c.get("/api/admin").json()["admin"] is True
        assert c.get("/api/admin/overview").status_code == 200

    def test_personal_mode_with_password_needs_login(self, downloads_dir, monkeypatch):
        monkeypatch.setattr(get_settings().server, "admin_password_hash", _HASH)
        c = TestClient(create_app())
        assert c.get("/api/admin/overview").status_code == 403
        assert c.post("/api/library/rename", json={"path": "x", "name": "y"}).status_code != 403  # the app itself stays open


class TestOverview:
    def test_counts_downloads_and_searches(self, app, monkeypatch):
        monkeypatch.setattr(metadata_service, "_extract", lambda url, flat: {"entries": []})
        v = visitor(app)
        v.post("/api/downloads", json={"urls": [WATCH]})
        v.get("/api/search", params={"q": "Some Song"})
        o = admin(app).get("/api/admin/overview").json()
        t = o["stats"]["today"]
        assert t["completed"] == 1 and t["bytes"] == 1000 and t["submitted"] == 1 and t["searches"] == 1 and t["visitors"] == 1
        assert o["stats"]["formats"] == {"mp3": 1}
        assert o["stats"]["top_videos"][0]["title"] == "Fake Song"
        assert o["stats"]["top_searches"] == [{"query": "some song", "count": 1}]
        assert o["mode"] == "server" and o["queue"]["completed"] == 1 and len(o["stats"]["hourly"]) == 24
        assert o["stats"]["online"] == 1

    def test_failures_are_listed(self, app):
        FakeYDL.fail_with = ValueError("boom")
        visitor(app).post("/api/downloads", json={"urls": [WATCH]})
        errors = admin(app).get("/api/admin/overview").json()["stats"]["errors"]
        assert errors and "boom" in errors[0]["error"]

    def test_analytics_never_break_requests(self, app, monkeypatch):
        monkeypatch.setattr(type(analytics), "_p", property(lambda self: (_ for _ in ()).throw(RuntimeError("redis down"))))
        assert visitor(app).post("/api/downloads", json={"urls": [WATCH]}).status_code == 201


class TestActivityAndBans:
    def test_jobs_of_every_visitor(self, app):
        a, b = visitor(app), visitor(app)
        ja = a.post("/api/downloads", json={"urls": [WATCH]}).json()["jobs"][0]["job_id"]
        b.post("/api/downloads", json={"urls": [WATCH]})
        adm = admin(app)
        jobs = adm.get("/api/admin/jobs").json()
        assert len(jobs) == 2 and {j["owner"][:4] for j in jobs} == {a.cookies.get(VISITOR_COOKIE)[:4], b.cookies.get(VISITOR_COOKIE)[:4]}
        assert adm.post("/api/admin/jobs/bulk", json={"ids": [ja], "action": "remove"}).json()["affected"] == 1
        assert a.get("/api/downloads").json() == []

    def test_visitors_and_bans(self, app):
        v = visitor(app)
        v.get("/api/downloads")
        adm = admin(app)
        vis = adm.get("/api/admin/visitors").json()
        assert len(vis) == 1 and vis[0]["requests"] >= 2 and vis[0]["online"]
        ip = vis[0]["ip"]
        assert adm.post("/api/admin/bans", json={"ip": ip, "reason": "abuse"}).status_code == 200
        assert v.get("/api/downloads").status_code == 403  # blocked
        assert adm.get("/api/admin/overview").status_code == 200  # the admin area is never blocked
        assert adm.get("/api/admin/bans").json()[0]["reason"] == "abuse"
        adm.delete(f"/api/admin/bans/{ip}")
        assert v.get("/api/downloads").status_code == 200

    def test_admin_traffic_is_not_a_visitor(self, app):
        admin(app).get("/api/admin/overview")
        assert admin(app).get("/api/admin/visitors").json() == []


class TestFiles:
    @pytest.fixture
    def tree(self, downloads_dir):
        (downloads_dir / "Music").mkdir()
        (downloads_dir / "Music" / "a.mp3").write_bytes(b"a" * 10)
        (downloads_dir / "notes.txt").write_text("hello", encoding="utf-8")
        (downloads_dir / ".deliveries" / "abc123def456").mkdir(parents=True)
        (downloads_dir / ".deliveries" / "abc123def456" / "x.mp3").write_bytes(b"x")
        return downloads_dir

    def test_list_includes_hidden(self, app, tree):
        adm = admin(app)
        names = [e["name"] for e in adm.get("/api/admin/files").json()]
        assert names == [".deliveries", "Music", "notes.txt"]
        assert ".deliveries" not in [e["name"] for e in adm.get("/api/admin/files", params={"hidden": False}).json()]
        assert adm.get("/api/admin/files", params={"path": ".deliveries/abc123def456"}).json()[0]["name"] == "x.mp3"
        usage = adm.get("/api/admin/files/usage").json()
        assert usage[0]["name"] == "Music" and usage[0]["size"] == 10

    def test_crud_and_batch(self, app, tree):
        adm = admin(app)
        assert adm.post("/api/admin/files/mkdir", json={"path": "New/Inner"}).status_code == 200
        assert adm.post("/api/admin/files/rename", json={"path": "notes.txt", "name": "readme"}).json()["name"] == "readme.txt"
        r = adm.post("/api/admin/files/copy", json={"paths": ["Music/a.mp3"], "dest": "New"}).json()
        assert r == {"done": 1, "errors": []} and (tree / "New" / "a.mp3").is_file() and (tree / "Music" / "a.mp3").is_file()
        assert adm.post("/api/admin/files/move", json={"paths": ["readme.txt", "Music"], "dest": "New/Inner"}).json()["done"] == 2
        assert (tree / "New" / "Inner" / "Music" / "a.mp3").is_file()
        assert adm.post("/api/admin/files/move", json={"paths": ["New"], "dest": "New/Inner"}).json()["errors"]  # not into itself
        assert adm.post("/api/admin/files/delete", json={"paths": [".deliveries", "New"]}).json()["done"] == 2
        assert [p.name for p in tree.iterdir()] == []

    def test_read_zip_download(self, app, tree):
        adm = admin(app)
        assert adm.get("/api/admin/files/text", params={"path": "notes.txt"}).text == "hello"
        assert adm.get("/api/admin/files/text", params={"path": "Music/a.mp3"}).status_code == 422
        assert adm.get("/api/admin/files/download", params={"path": "Music/a.mp3"}).content == b"a" * 10
        z = zipfile.ZipFile(io.BytesIO(adm.get("/api/admin/files/zip", params={"path": [".deliveries", "Music"]}).content))
        assert sorted(z.namelist()) == [".deliveries/abc123def456/x.mp3", "Music/a.mp3"]

    @pytest.mark.parametrize("bad", ["../outside", "..", "Music/../../x"])
    def test_cannot_escape_the_root(self, app, tree, bad):
        adm = admin(app)
        assert adm.get("/api/admin/files", params={"path": bad}).status_code == 422
        assert adm.post("/api/admin/files/delete", json={"paths": [bad]}).json()["errors"]

    def test_root_is_protected(self, app, tree):
        adm = admin(app)
        assert adm.post("/api/admin/files/delete", json={"paths": [""]}).json()["errors"]
        assert adm.post("/api/admin/files/rename", json={"path": "", "name": "x"}).status_code == 422


class TestMaintenance:
    def test_every_action_runs(self, app):
        adm = admin(app)
        actions = [a["action"] for a in adm.get("/api/admin/maintenance").json()]
        assert "clear-meta-cache" in actions and "sweep-now" in actions
        for a in actions:
            r = adm.post(f"/api/admin/maintenance/{a}")
            assert r.status_code == 200, (a, r.text)
        assert adm.post("/api/admin/maintenance/nope").status_code == 422

    def test_cache_clear_drops_keys(self, app, fake_redis):
        fake_redis.set(f"{get_settings().redis.redis_key_prefix}:META:x", "1")
        assert admin(app).post("/api/admin/maintenance/clear-meta-cache").json()["affected"] == 1

    def test_maintenance_mode_refuses_new_downloads(self, app):
        adm = admin(app)
        adm.put("/api/admin/config", json={"values": {"server.maintenance": True, "server.maintenance_message": "Back at 6"}, "reset": []})
        try:
            r = visitor(app).post("/api/downloads", json={"urls": [WATCH]})
            assert r.status_code == 503 and r.json()["detail"] == "Back at 6"
        finally:
            adm.put("/api/admin/config", json={"values": {}, "reset": ["server.maintenance", "server.maintenance_message"]})
        assert visitor(app).post("/api/downloads", json={"urls": [WATCH]}).status_code == 201


class TestLogs:
    def test_tail_filter_and_safety(self, app, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        (tmp_path / "worker.log").write_text("a INFO one\nb WARNING two\nc ERROR three\n", encoding="utf-8")
        adm = admin(app)
        assert [f["name"] for f in adm.get("/api/admin/logs").json()] == ["worker.log"]
        assert adm.get("/api/admin/logs/worker.log", params={"level": "warning"}).json()["lines"] == ["b WARNING two", "c ERROR three"]
        assert adm.get("/api/admin/logs/worker.log", params={"q": "three"}).json()["lines"] == ["c ERROR three"]
        assert adm.get("/api/admin/logs/..%2F.env").status_code in (404, 422)
        assert adm.get("/api/admin/logs/nope.log").status_code == 404
