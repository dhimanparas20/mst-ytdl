"""SERVER_MODE: per-visitor queues, deliveries (file / ZIP) + expiry, media cache, admin gate, limits.

Also the personal-mode side of the new features (download cart, job "Save" endpoint, storage totals),
which must behave exactly as before when SERVER_MODE is off.
"""

from __future__ import annotations

import io
import os
import time
import zipfile
from collections import namedtuple

import pytest
from fastapi.testclient import TestClient

import services.downloader as dl_mod
import services.manager as manager_mod
from api.main import create_app
from configs import get_settings
from models import EntryInfo
from modules.passwords import hash_password, is_valid_hash, verify_password
from services import delivery_service, download_manager, job_store, metadata_service
from services.access import ADMIN_COOKIE, VISITOR_COOKIE
from services.metadata import PlaylistInfo
from tests.fakes import FakeYDL

VID = "dQw4w9WgXcQ"
WATCH = f"https://www.youtube.com/watch?v={VID}"
PLAYLIST = "https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"
PASSWORD = "s3cret-admin-pw"
_HASH = hash_password(PASSWORD)  # scrypt is slow-ish on purpose: hash once per module


def _entries(n: int) -> list[EntryInfo]:
    return [EntryInfo(index=i, video_id=f"{i:011d}", url=f"https://www.youtube.com/watch?v={i:011d}", title=f"T{i}")
            for i in range(1, n + 1)]


@pytest.fixture
def server(monkeypatch, downloads_dir, fake_ydl):
    """SERVER_MODE=true with an admin password, a 60-minute TTL and generous limits."""
    s = get_settings().server
    monkeypatch.setattr(s, "mode", True)
    monkeypatch.setattr(s, "admin_password_hash", _HASH)
    monkeypatch.setattr(s, "delivery_ttl_minutes", 60)
    monkeypatch.setattr(s, "max_active_jobs", 50)
    monkeypatch.setattr(s, "max_job_items", 100)
    monkeypatch.setattr(s, "min_free_mb", 0)
    monkeypatch.setattr(s, "rate_limit_per_minute", 1000)
    monkeypatch.setattr(dl_mod, "_CHECK_INTERVAL", 0.0)
    return s


@pytest.fixture
def app(server):
    return create_app()  # built after the switch: the visitor middleware is registered at creation


def visitor(app) -> TestClient:
    """A browser with its own cookie jar (= its own visitor id)."""
    c = TestClient(app)
    c.get("/api/config")  # receives the visitor cookie
    assert c.cookies.get(VISITOR_COOKIE)
    return c


def post(c, urls=None, **kw):
    return c.post("/api/downloads", json={"urls": urls or [WATCH], **kw})


# ───────────────────────── passwords ─────────────────────────


class TestPasswords:
    def test_roundtrip_and_format(self):
        assert is_valid_hash(_HASH) and _HASH.startswith("scrypt:") and "$" not in _HASH
        assert verify_password(PASSWORD, _HASH)
        assert not verify_password("wrong", _HASH)

    @pytest.mark.parametrize("bad", [None, "", "scrypt:1:2", "bcrypt:16384:8:1:aa:bb", "scrypt:x:8:1:aa:bb"])
    def test_malformed_hashes_never_verify(self, bad):
        assert not verify_password(PASSWORD, bad)

    def test_empty_password_rejected(self):
        with pytest.raises(ValueError):
            hash_password("")


# ───────────────────────── visitors ─────────────────────────


class TestVisitorIsolation:
    def test_each_browser_sees_only_its_own_queue(self, app):
        a, b = visitor(app), visitor(app)
        assert a.cookies.get(VISITOR_COOKIE) != b.cookies.get(VISITOR_COOKIE)
        ja = post(a).json()["jobs"][0]["job_id"]
        jb = post(b).json()["jobs"][0]["job_id"]
        assert [j["job_id"] for j in a.get("/api/downloads").json()] == [ja]
        assert [j["job_id"] for j in b.get("/api/downloads").json()] == [jb]

    def test_other_visitors_jobs_and_items_are_404(self, app):
        a, b = visitor(app), visitor(app)
        job = post(a).json()["jobs"][0]
        item = job["items"][0]["item_id"]
        for r in (
            b.get(f"/api/downloads/{job['job_id']}"),
            b.post(f"/api/downloads/{job['job_id']}/cancel"),
            b.get(f"/api/downloads/{job['job_id']}/file"),
            b.delete(f"/api/downloads/{job['job_id']}"),
            b.get(f"/api/items/{item}"),
            b.get(f"/api/items/{item}/file"),
            b.post(f"/api/items/{item}/restart"),
        ):
            assert r.status_code == 404, r.request.url
        bulk = b.post("/api/downloads/bulk", json={"ids": [job["job_id"]], "action": "remove"}).json()
        assert bulk["affected"] == 0 and bulk["errors"]
        assert a.get(f"/api/downloads/{job['job_id']}").status_code == 200

    def test_queue_actions_only_touch_your_jobs(self, app):
        a, b = visitor(app), visitor(app)
        post(a)
        post(b)
        assert a.post("/api/queue/clear-finished").json()["affected"] == 1
        assert a.get("/api/downloads").json() == []
        assert len(b.get("/api/downloads").json()) == 1

    def test_stats_are_per_visitor(self, app):
        a, b = visitor(app), visitor(app)
        post(a)
        post(a)
        owner = a.cookies.get(VISITOR_COOKIE)
        assert download_manager.stats(owner=owner).total_jobs == 2
        assert download_manager.stats(owner=b.cookies.get(VISITOR_COOKIE)).total_jobs == 0

    def test_cookie_is_httponly(self, server):
        r = TestClient(create_app()).get("/api/config")
        assert "httponly" in r.headers["set-cookie"].lower()

    def test_personal_mode_sets_no_cookie(self, downloads_dir, fake_ydl):
        r = TestClient(create_app()).get("/api/config")
        assert "set-cookie" not in r.headers


# ───────────────────────── deliveries ─────────────────────────


class TestDeliveries:
    def test_single_video_is_served_as_the_file(self, app, downloads_dir):
        c = visitor(app)
        jid = post(c).json()["jobs"][0]["job_id"]
        view = c.get(f"/api/downloads/{jid}").json()
        assert view["status"] == "completed"
        assert view["folder"] == f".deliveries/{jid}"
        assert view["delivery"]["kind"] == "file" and view["delivery"]["name"] == "Fake Song.mp3"
        assert view["delivery"]["expires_at"] == pytest.approx(time.time() + 3600, abs=30)
        r = c.get(f"/api/downloads/{jid}/file")
        assert r.status_code == 200 and len(r.content) == 1000
        assert "attachment" in r.headers["content-disposition"] and r.headers.get("accept-ranges") == "bytes"
        assert (downloads_dir / ".deliveries" / jid / "Fake Song.mp3").is_file()

    def test_playlist_becomes_one_zip(self, app, downloads_dir, monkeypatch):
        monkeypatch.setattr(metadata_service, "expand_playlist", lambda u: PlaylistInfo("Road Trip", "DJ", None, _entries(3)))
        c = visitor(app)
        jid = post(c, [PLAYLIST]).json()["jobs"][0]["job_id"]
        view = c.get(f"/api/downloads/{jid}").json()
        assert view["status"] == "completed" and view["folder"] == f".deliveries/{jid}/Road Trip"
        assert view["delivery"]["kind"] == "zip" and view["delivery"]["name"] == "Road Trip.zip"
        z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/downloads/{jid}/file").content))
        assert sorted(z.namelist()) == [f"Road Trip/0{i} - Fake Song.mp3" for i in (1, 2, 3)]
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())

    def test_partial_playlist_zips_what_finished(self, app, monkeypatch):
        monkeypatch.setattr(metadata_service, "expand_playlist", lambda u: PlaylistInfo("Mixed", None, None, _entries(3)))
        orig = dl_mod.DownloadRunner._download

        def flaky(self, ctx):
            if ctx.item.index == 2:
                raise ValueError("boom")  # non-retryable
            return orig(self, ctx)

        monkeypatch.setattr(dl_mod.DownloadRunner, "_download", flaky)
        c = visitor(app)
        jid = post(c, [PLAYLIST]).json()["jobs"][0]["job_id"]
        assert c.get(f"/api/downloads/{jid}").json()["status"] == "partial"
        names = zipfile.ZipFile(io.BytesIO(c.get(f"/api/downloads/{jid}/file").content)).namelist()
        assert sorted(names) == ["Mixed/01 - Fake Song.mp3", "Mixed/03 - Fake Song.mp3"]

    def test_failed_job_has_no_file_but_is_scheduled_for_cleanup(self, app):
        FakeYDL.fail_with = ValueError("nope")
        c = visitor(app)
        jid = post(c).json()["jobs"][0]["job_id"]
        assert c.get(f"/api/downloads/{jid}").json()["delivery"] is None
        assert c.get(f"/api/downloads/{jid}/file").status_code == 404
        assert delivery_service.get(jid).kind.value == "none"

    def test_running_job_file_is_409(self, app, dispatcher):
        c = visitor(app)
        jid = post(c).json()["jobs"][0]["job_id"]  # the fake dispatcher never runs it
        assert c.get(f"/api/downloads/{jid}/file").status_code == 409

    def test_cancelling_the_rest_builds_the_zip(self, app, monkeypatch, dispatcher):
        monkeypatch.setattr(metadata_service, "expand_playlist", lambda u: PlaylistInfo("Half", None, None, _entries(2)))
        c = visitor(app)
        jid = post(c, [PLAYLIST]).json()["jobs"][0]["job_id"]
        download_manager.resolve_job(jid)
        first, second = job_store.get_items(jid)
        dl_mod.download_runner.run(first.item_id, first.token)
        c.post(f"/api/items/{second.item_id}/cancel")
        assert jid in dispatcher.deliveries  # the worker is asked to prepare the ZIP
        assert delivery_service.maybe_build(jid).kind.value == "zip"

    def test_item_file_endpoint(self, app):
        c = visitor(app)
        job = post(c).json()["jobs"][0]
        r = c.get(f"/api/items/{job['items'][0]['item_id']}/file")
        assert r.status_code == 200 and "inline" in r.headers["content-disposition"]

    def test_same_title_jobs_never_share_files(self, app, downloads_dir):
        a, b = visitor(app), visitor(app)
        ja = post(a).json()["jobs"][0]["job_id"]
        jb = post(b).json()["jobs"][0]["job_id"]
        fa = downloads_dir / ".deliveries" / ja / "Fake Song.mp3"
        fb = downloads_dir / ".deliveries" / jb / "Fake Song.mp3"
        assert fa.is_file() and fb.is_file()
        a.delete(f"/api/downloads/{ja}")
        assert not fa.exists() and fb.is_file()  # removing one visitor's job never touches the other's

    def test_deliveries_are_hidden_from_the_library(self, app):
        c = visitor(app)
        jid = post(c).json()["jobs"][0]["job_id"]
        assert c.get("/api/library").json() == []
        assert c.get("/api/library/file", params={"path": f".deliveries/{jid}/Fake Song.mp3"}).status_code == 422
        assert c.get("/api/library/usage").json()["files"] == 0


class TestMediaCache:
    def test_second_request_for_the_same_file_is_not_downloaded_again(self, app, downloads_dir):
        a, b = visitor(app), visitor(app)
        post(a)
        assert FakeYDL.calls == 1
        jb = post(b).json()["jobs"][0]
        assert jb["status"] == "completed" and FakeYDL.calls == 1  # served from the cache
        assert (downloads_dir / ".deliveries" / jb["job_id"] / "Fake Song.mp3").stat().st_size == 1000

    def test_different_options_miss_the_cache(self, app):
        c = visitor(app)
        post(c, format="mp3", quality="192")
        post(c, format="mp3", quality="320")
        assert FakeYDL.calls == 2

    def test_restart_bypasses_the_cache(self, app):
        c = visitor(app)
        job = post(c).json()["jobs"][0]
        c.post(f"/api/items/{job['items'][0]['item_id']}/restart")
        assert FakeYDL.calls == 2

    def test_cache_survives_the_source_job_being_swept(self, app, downloads_dir):
        a, b = visitor(app), visitor(app)
        ja = post(a).json()["jobs"][0]["job_id"]
        jb = post(b).json()["jobs"][0]["job_id"]  # hard link / copy of a's file
        delivery_service.purge(ja)
        assert (downloads_dir / ".deliveries" / jb / "Fake Song.mp3").stat().st_size == 1000
        post(visitor(app))
        assert FakeYDL.calls == 2  # a's entry pointed at a deleted file → a real download

    def test_personal_mode_never_uses_it(self, downloads_dir, fake_ydl):
        c = TestClient(create_app())
        post(c, overwrite=True)
        post(c, overwrite=True)
        assert FakeYDL.calls == 2


# ───────────────────────── expiry ─────────────────────────


class TestSweep:
    def test_expired_jobs_are_purged_completely(self, app, downloads_dir):
        c = visitor(app)
        jid = post(c).json()["jobs"][0]["job_id"]
        item_id = job_store.get_item_ids(jid)[0]
        assert delivery_service.sweep(now=time.time() + 60)["expired"] == 0  # not yet
        assert delivery_service.sweep(now=time.time() + 3601)["expired"] == 1
        assert job_store.get_job(jid) is None and job_store.get_item(item_id) is None
        assert not (downloads_dir / ".deliveries" / jid).exists()
        assert c.get("/api/downloads").json() == []

    def test_only_expired_jobs_go(self, app, downloads_dir):
        c = visitor(app)
        old = post(c).json()["jobs"][0]["job_id"]
        d = delivery_service.get(old)
        delivery_service._save(d.model_copy(update={"expires_at": time.time() - 1}))
        new = post(c, format="m4a").json()["jobs"][0]["job_id"]
        delivery_service.sweep()
        assert job_store.get_job(old) is None and job_store.get_job(new) is not None
        assert (downloads_dir / ".deliveries" / new).is_dir()

    def test_missing_delivery_is_built_by_the_sweep(self, app, fake_redis):
        c = visitor(app)
        jid = post(c).json()["jobs"][0]["job_id"]
        fake_redis.delete(delivery_service._key(jid))  # as if the worker died before writing the record
        fake_redis.zrem(delivery_service._index, jid)
        assert c.get(f"/api/downloads/{jid}").json()["delivery"] is None
        assert delivery_service.sweep()["built"] == 1
        assert c.get(f"/api/downloads/{jid}").json()["delivery"]["kind"] == "file"

    def test_lost_files_give_an_empty_delivery(self, app):
        c = visitor(app)
        jid = post(c).json()["jobs"][0]["job_id"]
        delivery_service.forget(jid)  # record AND files gone
        assert delivery_service.sweep()["built"] == 1 and delivery_service.get(jid).kind.value == "none"

    def test_idle_paused_jobs_are_dropped(self, app, dispatcher):
        c = visitor(app)
        job = post(c).json()["jobs"][0]
        c.post(f"/api/items/{job['items'][0]['item_id']}/pause")
        assert delivery_service.sweep(now=time.time() + 60)["idle"] == 0
        assert delivery_service.sweep(now=time.time() + 3700)["idle"] == 1
        assert job_store.get_job(job["job_id"]) is None

    def test_orphan_directories_are_removed_once_old(self, server, downloads_dir):
        orphan = downloads_dir / ".deliveries" / "abcdef123456"
        orphan.mkdir(parents=True)
        (orphan / "x.mp3").write_bytes(b"x")
        tmp_orphan = downloads_dir / ".tmp" / "fedcba654321"
        tmp_orphan.mkdir(parents=True)
        keep = downloads_dir / ".deliveries" / "not-an-id"
        keep.mkdir()
        assert delivery_service.sweep()["orphans"] == 0  # too young
        assert delivery_service.sweep(now=time.time() + 3700)["orphans"] == 2
        assert not orphan.exists() and not tmp_orphan.exists() and keep.exists()

    def test_personal_mode_jobs_are_never_swept(self, downloads_dir, fake_ydl, monkeypatch):
        c = TestClient(create_app())
        jid = post(c).json()["jobs"][0]["job_id"]
        monkeypatch.setattr(get_settings().server, "mode", True)
        delivery_service.sweep(now=time.time() + 10**7)
        assert job_store.get_job(jid) is not None

    def test_sweep_is_a_noop_in_personal_mode(self, downloads_dir):
        assert delivery_service.sweep() == {}

    def test_bad_ids_never_resolve_to_a_path(self, server):
        for bad in ("../x", "", "ABC", "a/b"):
            with pytest.raises(ValueError):
                delivery_service.job_dir(bad)


# ───────────────────────── admin ─────────────────────────


class TestAdmin:
    def test_management_needs_the_admin(self, app, downloads_dir):
        (downloads_dir / "Album").mkdir()
        (downloads_dir / "Album" / "a.mp3").write_bytes(b"x")
        c = visitor(app)
        assert c.get("/api/library").status_code == 200  # reading is open
        assert c.get("/api/library/file", params={"path": "Album/a.mp3"}).status_code == 200
        assert c.delete("/api/library", params={"path": "Album/a.mp3"}).status_code == 403
        assert c.post("/api/library/rename", json={"path": "Album", "name": "B"}).status_code == 403
        assert c.post("/api/library/move", json={"paths": ["Album"], "dest": "X"}).status_code == 403
        assert c.post("/api/library/bulk-delete", json={"paths": ["Album"]}).status_code == 403
        assert c.put("/api/runtime", json={"max_parallel": 2}).status_code == 403
        assert c.put("/api/cookies", json={"text": "x"}).status_code == 403
        assert c.delete("/api/cookies").status_code == 403
        assert (downloads_dir / "Album" / "a.mp3").exists()

    def test_login_unlocks_and_logout_locks(self, app, downloads_dir):
        (downloads_dir / "a.mp3").write_bytes(b"x")
        c = visitor(app)
        assert c.get("/api/admin").json() == {"server_mode": True, "enabled": True, "admin": False, "username": None}
        assert c.post("/api/admin/login", json={"username": "admin", "password": "nope"}).status_code == 403
        r = c.post("/api/admin/login", json={"username": "admin", "password": PASSWORD})
        assert r.status_code == 200 and r.json()["admin"] is True
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert c.delete("/api/library", params={"path": "a.mp3"}).status_code == 200
        assert c.put("/api/runtime", json={"max_parallel": 2}).status_code == 200
        c.post("/api/admin/logout")
        assert c.get("/api/admin").json()["admin"] is False
        assert c.put("/api/runtime", json={"max_parallel": 3}).status_code == 403

    def test_forged_session_is_rejected(self, app):
        c = visitor(app)
        c.cookies.set(ADMIN_COOKIE, "A" * 43)
        assert c.put("/api/runtime", json={"max_parallel": 2}).status_code == 403

    def test_lockout_after_repeated_failures(self, app):
        c = visitor(app)
        for _ in range(8):
            assert c.post("/api/admin/login", json={"username": "admin", "password": "bad"}).status_code == 403
        assert c.post("/api/admin/login", json={"username": "admin", "password": PASSWORD}).status_code == 429

    def test_no_hash_configured_means_locked(self, app, server, monkeypatch):
        monkeypatch.setattr(server, "admin_password_hash", None)
        c = visitor(app)
        assert c.get("/api/admin").json()["enabled"] is False
        assert c.post("/api/admin/login", json={"username": "admin", "password": PASSWORD}).status_code == 403
        assert c.put("/api/runtime", json={"max_parallel": 2}).status_code == 403

    def test_personal_mode_is_open(self, downloads_dir, fake_ydl):
        c = TestClient(create_app())
        assert c.get("/api/admin").json() == {"server_mode": False, "enabled": False, "admin": True, "username": None}
        assert c.put("/api/runtime", json={"max_parallel": 2}).status_code == 200


# ───────────────────────── limits ─────────────────────────


class TestLimits:
    def test_active_job_cap_per_visitor(self, app, server, monkeypatch, dispatcher):
        monkeypatch.setattr(server, "max_active_jobs", 2)
        a, b = visitor(app), visitor(app)
        assert post(a, [WATCH, "https://youtu.be/aaaaaaaaaaa"]).status_code == 201
        r = post(a)
        assert r.status_code == 429 and "2 unfinished" in r.json()["detail"]
        assert post(b).status_code == 201  # another visitor is unaffected

    def test_low_disk_refuses_new_work(self, app, server, monkeypatch):
        Usage = namedtuple("Usage", "total used free")
        monkeypatch.setattr(manager_mod.shutil, "disk_usage", lambda p: Usage(10, 9, 1024))
        monkeypatch.setattr(server, "min_free_mb", 100)
        assert post(visitor(app)).status_code == 503

    def test_playlist_too_big_fails_with_a_hint(self, app, server, monkeypatch):
        monkeypatch.setattr(server, "max_job_items", 2)
        monkeypatch.setattr(metadata_service, "expand_playlist", lambda u: PlaylistInfo("Big", None, None, _entries(3)))
        c = visitor(app)
        job = post(c, [PLAYLIST]).json()["jobs"][0]
        view = c.get(f"/api/downloads/{job['job_id']}").json()
        assert view["status"] == "failed" and "at most 2" in view["resolve_error"]
        assert post(c, [PLAYLIST], playlist_items="1-2").status_code == 201

    def test_folder_and_overwrite_are_ignored(self, app):
        c = visitor(app)
        job = post(c, folder="../../etc", overwrite=True).json()["jobs"][0]
        assert job["folder"] == f".deliveries/{job['job_id']}" and job["options"]["overwrite"] is False

    def test_rate_limit(self, app, server, monkeypatch):
        monkeypatch.setattr(server, "rate_limit_per_minute", 2)
        monkeypatch.setattr(metadata_service, "_extract", lambda url, flat: {"entries": []})
        c = visitor(app)
        codes = [c.get("/api/search", params={"q": "abc"}).status_code for _ in range(3)]
        assert codes == [200, 200, 429]


# ───────────────────────── cart ─────────────────────────


def cart(c, n=3, **kw):
    entries = [{"url": f"https://www.youtube.com/watch?v={i:011d}", "title": f"Song {i}"} for i in range(1, n + 1)]
    return c.post("/api/cart/checkout", json={"entries": entries, **kw})


class TestCart:
    def test_server_mode_cart_is_one_zip(self, app):
        c = visitor(app)
        r = cart(c, name="Party")
        assert r.status_code == 201
        job = r.json()["job"]
        assert job["kind"] == "cart" and job["total_items"] == 3 and job["deliver_zip"] is True
        view = c.get(f"/api/downloads/{job['job_id']}").json()
        assert view["status"] == "completed" and view["delivery"]["name"] == "Party.zip"
        names = zipfile.ZipFile(io.BytesIO(c.get(f"/api/downloads/{job['job_id']}/file").content)).namelist()
        assert sorted(names) == ["Party/01 - Fake Song.mp3", "Party/02 - Fake Song.mp3", "Party/03 - Fake Song.mp3"]

    def test_server_mode_cart_cap(self, app, server, monkeypatch):
        monkeypatch.setattr(server, "max_job_items", 2)
        assert cart(visitor(app), n=3).status_code == 422

    def test_personal_mode_cart_lands_in_a_named_folder(self, downloads_dir, fake_ydl, monkeypatch):
        monkeypatch.setattr(dl_mod, "_CHECK_INTERVAL", 0.0)
        c = TestClient(create_app())
        r = cart(c, name="Road Mix")
        job = r.json()["job"]
        assert job["folder"] == "Road Mix" and job["deliver_zip"] is False
        assert sorted(p.name for p in (downloads_dir / "Road Mix").iterdir()) == [
            "01 - Fake Song.mp3", "02 - Fake Song.mp3", "03 - Fake Song.mp3"]
        titles = [i["title"] for i in c.get(f"/api/downloads/{job['job_id']}").json()["items"]]
        assert titles == ["Song 1", "Song 2", "Song 3"]  # the cart's titles are kept

    def test_personal_mode_zip_option_streams_a_zip(self, downloads_dir, fake_ydl, monkeypatch):
        monkeypatch.setattr(dl_mod, "_CHECK_INTERVAL", 0.0)
        c = TestClient(create_app())
        job = cart(c, n=2, name="Zipped", zip=True).json()["job"]
        assert job["deliver_zip"] is True
        r = c.get(f"/api/downloads/{job['job_id']}/file")
        assert r.headers["content-type"] == "application/zip"
        assert sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == ["Zipped/01 - Fake Song.mp3", "Zipped/02 - Fake Song.mp3"]
        assert not list(downloads_dir.rglob("*.zip"))  # streamed, nothing left on disk

    def test_default_name_and_bad_entries(self, downloads_dir, fake_ydl):
        c = TestClient(create_app())
        r = c.post("/api/cart/checkout", json={"entries": [
            {"url": WATCH}, {"url": WATCH}, {"url": "https://example.com/x"},
        ]})
        body = r.json()
        assert r.status_code == 201 and body["job"]["total_items"] == 1  # duplicate dropped
        assert body["rejected"][0]["url"] == "https://example.com/x"
        assert body["job"]["title"].startswith("Cart ")

    def test_all_bad_is_422(self, downloads_dir, fake_ydl):
        c = TestClient(create_app())
        assert c.post("/api/cart/checkout", json={"entries": [{"url": "https://example.com/x"}]}).status_code == 422

    def test_personal_single_job_file(self, downloads_dir, fake_ydl):
        c = TestClient(create_app())
        jid = post(c).json()["jobs"][0]["job_id"]
        r = c.get(f"/api/downloads/{jid}/file")
        assert r.status_code == 200 and len(r.content) == 1000 and "Fake%20Song.mp3" in r.headers["content-disposition"]


class TestConfig:
    def test_config_reports_server_mode(self, app):
        cfg = visitor(app).get("/api/config").json()
        assert cfg["server_mode"] is True and cfg["admin_enabled"] is True
        assert cfg["delivery_ttl_minutes"] == 60 and cfg["max_job_items"] == 100
        assert "/" not in cfg["downloads_dir"] and "\\" not in cfg["downloads_dir"]  # no absolute path for the public

    def test_personal_config_unchanged(self, downloads_dir):
        cfg = TestClient(create_app()).get("/api/config").json()
        assert cfg["server_mode"] is False and cfg["delivery_ttl_minutes"] == 0
        assert cfg["downloads_dir"] == str(downloads_dir)


# ───────────────────────── my downloads / keep longer / maintenance ─────────────────────────


class TestMyDownloads:
    def test_lists_only_the_visitors_ready_files(self, app, monkeypatch):
        monkeypatch.setattr(metadata_service, "expand_playlist", lambda u: PlaylistInfo("Road Trip", "DJ", None, _entries(2)))
        a, b = visitor(app), visitor(app)
        single = post(a).json()["jobs"][0]["job_id"]
        zipped = post(a, [PLAYLIST]).json()["jobs"][0]["job_id"]
        post(b)
        rows = {r["job_id"]: r for r in a.get("/api/deliveries").json()}
        assert set(rows) == {single, zipped}
        assert rows[single]["kind"] == "file" and rows[single]["name"] == "Fake Song.mp3" and rows[single]["size"] == 1000
        assert rows[single]["status"] == "completed" and rows[single]["extended"] is False and rows[single]["title"]
        assert rows[zipped]["kind"] == "zip" and rows[zipped]["name"] == "Road Trip.zip"
        assert rows[single]["expires_at"] == pytest.approx(time.time() + 3600, abs=30)
        assert len(b.get("/api/deliveries").json()) == 1
        assert not set(rows) & {r["job_id"] for r in b.get("/api/deliveries").json()}  # never another visitor's

    def test_list_is_soonest_first_and_skips_expired_failed_and_missing(self, app, downloads_dir):
        c = visitor(app)
        late, soon, gone, expired = (post(c).json()["jobs"][0]["job_id"] for _ in range(4))
        d = delivery_service.get(soon)
        delivery_service._save(d.model_copy(update={"expires_at": d.expires_at - 1800}))
        d = delivery_service.get(expired)
        delivery_service._save(d.model_copy(update={"expires_at": time.time() - 5}))  # the sweep has not run yet
        (downloads_dir / ".deliveries" / gone / "Fake Song.mp3").unlink()
        FakeYDL.fail_with = ValueError("nope")
        post(c, format="opus")  # failed → NONE record (another format: the mp3 is in the media cache)
        assert [r["job_id"] for r in c.get("/api/deliveries").json()] == [soon, late]

    def test_extend_adds_one_ttl_once(self, app, fake_redis):
        c = visitor(app)
        jid = post(c).json()["jobs"][0]["job_id"]
        before = delivery_service.get(jid).expires_at
        r = c.post(f"/api/deliveries/{jid}/extend")
        assert r.status_code == 200 and r.json()["extended"] is True
        assert r.json()["expires_at"] == pytest.approx(before + 3600)
        assert fake_redis.zscore(delivery_service._index, jid) == pytest.approx(before + 3600)  # the sweep honours it
        assert fake_redis.ttl(delivery_service._key(jid)) > 3600
        view = c.get(f"/api/downloads/{jid}").json()["delivery"]
        assert view["extended"] is True and view["expires_at"] == pytest.approx(before + 3600)
        assert c.get("/api/deliveries").json()[0]["extended"] is True
        again = c.post(f"/api/deliveries/{jid}/extend")
        assert again.status_code == 409
        assert delivery_service.get(jid).expires_at == pytest.approx(before + 3600)  # unchanged by the refusal

    def test_extended_delivery_survives_the_original_deadline(self, app):
        c = visitor(app)
        jid = post(c).json()["jobs"][0]["job_id"]
        c.post(f"/api/deliveries/{jid}/extend")
        assert delivery_service.sweep(now=time.time() + 3601)["expired"] == 0
        assert c.get(f"/api/downloads/{jid}/file").status_code == 200
        assert delivery_service.sweep(now=time.time() + 7201)["expired"] == 1

    def test_extend_is_owner_checked_and_needs_a_ready_file(self, app, dispatcher):
        a, b = visitor(app), visitor(app)
        jid = post(a).json()["jobs"][0]["job_id"]  # the fake dispatcher never runs it: no delivery yet
        assert a.post(f"/api/deliveries/{jid}/extend").status_code == 404
        assert a.post("/api/deliveries/" + "0" * 12 + "/extend").status_code == 404
        assert b.post(f"/api/deliveries/{jid}/extend").status_code == 404

    def test_other_visitor_cannot_extend(self, app):
        a, b = visitor(app), visitor(app)
        jid = post(a).json()["jobs"][0]["job_id"]
        before = delivery_service.get(jid).expires_at
        assert b.post(f"/api/deliveries/{jid}/extend").status_code == 404
        assert delivery_service.get(jid).expires_at == before and not delivery_service.get(jid).extended

    def test_expired_or_failed_delivery_cannot_be_extended(self, app):
        c = visitor(app)
        jid = post(c).json()["jobs"][0]["job_id"]
        d = delivery_service.get(jid)
        delivery_service._save(d.model_copy(update={"expires_at": time.time() - 1}))
        assert c.post(f"/api/deliveries/{jid}/extend").status_code == 404
        FakeYDL.fail_with = ValueError("nope")
        bad = post(c, format="opus").json()["jobs"][0]["job_id"]  # another format: the mp3 is in the media cache
        assert c.post(f"/api/deliveries/{bad}/extend").status_code == 404

    def test_personal_mode_has_no_deliveries(self, downloads_dir, fake_ydl):
        c = TestClient(create_app())
        assert c.get("/api/deliveries").json() == []
        jid = c.post("/api/downloads", json={"urls": [WATCH]}).json()["jobs"][0]["job_id"]
        assert c.post(f"/api/deliveries/{jid}/extend").status_code == 422


class TestMaintenanceConfig:
    def test_config_exposes_maintenance_in_server_mode(self, app, server, monkeypatch):
        cfg = visitor(app).get("/api/config").json()
        assert cfg["maintenance"] is False and cfg["maintenance_message"] == ""
        monkeypatch.setattr(server, "maintenance", True)
        monkeypatch.setattr(server, "maintenance_message", "Back at noon")
        c = visitor(app)
        cfg = c.get("/api/config").json()
        assert cfg["maintenance"] is True and cfg["maintenance_message"] == "Back at noon"
        r = post(c)  # the banner is not cosmetic: new downloads are refused
        assert r.status_code == 503 and "Back at noon" in r.json()["detail"]

    def test_personal_mode_never_reports_maintenance(self, downloads_dir, monkeypatch):
        monkeypatch.setattr(get_settings().server, "maintenance", True)  # ignored outside server mode
        cfg = TestClient(create_app()).get("/api/config").json()
        assert cfg["maintenance"] is False and cfg["maintenance_message"] == ""


# ───────────────────────── visitor cookies ─────────────────────────

FUTURE = int(time.time()) + 86_400 * 200


def _netscape(value: str = "secret-sid") -> str:
    rows = [("SID", value), ("__Secure-3PSID", "x"), ("LOGIN_INFO", "y"), ("PREF", "z")]
    return "# Netscape HTTP Cookie File\n" + "\n".join(
        f".youtube.com\tTRUE\t/\tTRUE\t{FUTURE}\t{n}\t{v}" for n, v in rows) + "\n.example.com\tTRUE\t/\tTRUE\t0\tX\tdrop"


@pytest.fixture
def server_cookie_file(tmp_path, monkeypatch):
    """A server (admin) cookies file that exists — visitors must not get it by default."""
    path = tmp_path / "ck" / "cookies.txt"
    path.parent.mkdir()
    path.write_text(_netscape("ADMIN-ONLY"), encoding="utf-8")
    monkeypatch.setattr(get_settings().download, "cookies_file", path)
    monkeypatch.setattr(get_settings().download, "cookies_from_browser", None)
    return path


def _cookiefile_now() -> str | None:
    from services.ytdl_opts import base_extract_opts
    return base_extract_opts().get("cookiefile")


class TestVisitorCookies:
    def test_save_status_and_remove(self, app, server_cookie_file, fake_redis):
        c = visitor(app)
        assert c.get("/api/my-cookies").json()["loaded"] is False
        st = c.put("/api/my-cookies", json={"text": _netscape()}).json()
        assert st["loaded"] and st["login"] and st["scope"] == "visitor" and st["count"] == 4
        assert any("other sites" in w for w in st["warnings"])
        assert "secret-sid" not in str(st)  # values are never sent back
        key = next(k for k in fake_redis.keys("*VCOOKIES*"))
        assert 0 < fake_redis.ttl(key) <= 24 * 3600 and st["deleted_at"]
        assert c.delete("/api/my-cookies").json()["loaded"] is False
        assert not fake_redis.keys("*VCOOKIES*")

    def test_bad_text_is_422(self, app, server_cookie_file):
        assert visitor(app).put("/api/my-cookies", json={"text": "nope"}).status_code == 422

    def test_scope_uses_only_your_cookies(self, app, server_cookie_file):
        from services import cookie_service
        a, b = visitor(app), visitor(app)
        a.put("/api/my-cookies", json={"text": _netscape("A-SECRET")})
        va, vb = a.cookies.get(VISITOR_COOKIE), b.cookies.get(VISITOR_COOKIE)
        with cookie_service.scope(va):
            path = _cookiefile_now()
            text = open(path, encoding="utf-8").read()
            assert "A-SECRET" in text and "ADMIN-ONLY" not in text and path != str(server_cookie_file)
        assert not os.path.exists(path)  # the temp copy is gone after the call
        with cookie_service.scope(vb):
            assert _cookiefile_now() is None  # b has none, and never gets a's or the server's
        assert _cookiefile_now() == str(server_cookie_file)  # outside any scope: the server's, as before

    def test_server_cookies_can_be_shared_on_purpose(self, app, server, server_cookie_file, monkeypatch):
        from services import cookie_service
        monkeypatch.setattr(server, "share_server_cookies", True)
        with cookie_service.scope(visitor(app).cookies.get(VISITOR_COOKIE)):
            assert _cookiefile_now() == str(server_cookie_file)

    def test_refreshed_cookies_are_written_back(self, app, server_cookie_file):
        from services import cookie_service
        c = visitor(app)
        c.put("/api/my-cookies", json={"text": _netscape("OLD")})
        v = c.cookies.get(VISITOR_COOKIE)
        with cookie_service.scope(v):
            p = _cookiefile_now()
            open(p, "w", encoding="utf-8").write(_netscape("ROTATED"))  # what yt-dlp does on close
        assert "ROTATED" in cookie_service.visitor_text(v)

    def test_caches_are_per_visitor_with_own_cookies(self, app, monkeypatch):
        calls = []

        def extract(url, flat):
            calls.append(url)
            return {"entries": [{"id": "a" * 11, "title": "Hit"}]}

        monkeypatch.setattr(metadata_service, "_extract", extract)
        anon1, anon2, own = visitor(app), visitor(app), visitor(app)
        own.put("/api/my-cookies", json={"text": _netscape()})
        anon1.get("/api/search", params={"q": "song"})
        anon2.get("/api/search", params={"q": "song"})
        assert len(calls) == 1  # anonymous lookups share one cache entry
        own.get("/api/search", params={"q": "song"})
        assert len(calls) == 2  # made with someone's cookies → cached for them only
        own.get("/api/search", params={"q": "song"})
        assert len(calls) == 2

    def test_download_runs_with_the_owners_cookies(self, app, server_cookie_file):
        c = visitor(app)
        c.put("/api/my-cookies", json={"text": _netscape("DL-SECRET")})
        post(c)
        used = FakeYDL.last_opts.get("cookiefile")
        assert used and used != str(server_cookie_file) and not os.path.exists(used)

    def test_download_without_own_cookies_uses_none(self, app, server_cookie_file):
        post(visitor(app))
        assert "cookiefile" not in FakeYDL.last_opts

    def test_personal_mode_has_no_visitor_cookies(self, downloads_dir, server_cookie_file):
        c = TestClient(create_app())
        assert c.get("/api/my-cookies").status_code == 422
        from services import cookie_service
        with cookie_service.scope("whoever"):
            assert _cookiefile_now() == str(server_cookie_file)

    def test_server_cookie_writes_stay_admin_only(self, app, server_cookie_file):
        c = visitor(app)
        assert c.put("/api/cookies", json={"text": _netscape()}).status_code == 403
        assert c.put("/api/my-cookies", json={"text": _netscape()}).status_code == 200

    def test_leftover_temp_files_are_swept(self, server, server_cookie_file):
        from services import cookie_service
        d = server_cookie_file.parent / "visitors"
        d.mkdir()
        old = d / "v-old.txt"
        old.write_text("x")
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        (d / "v-new.txt").write_text("x")
        assert cookie_service.sweep_scratch() == 1 and not old.exists()


# ───────────────────────── hard ceilings + live config ─────────────────────────


class TestCeilings:
    def _admin(self, app):
        c = visitor(app)
        c.post("/api/admin/login", json={"username": "admin", "password": PASSWORD})
        return c

    def test_parallel_above_the_cap_is_rejected(self, app, monkeypatch):
        monkeypatch.setattr(get_settings().task, "max_parallel_cap", 4)
        c = self._admin(app)
        assert c.put("/api/runtime", json={"max_parallel": 5}).status_code == 422
        assert c.put("/api/runtime", json={"max_parallel": 4}).json()["max_parallel_cap"] == 4

    def test_speed_ceiling_rejects_and_caps(self, app, server, monkeypatch):
        from services.ytdl_opts import effective_speed
        monkeypatch.setattr(server, "max_speed", 1_000_000)
        c = self._admin(app)
        assert c.put("/api/runtime", json={"speed_limit": 2_000_000}).status_code == 422
        assert c.put("/api/runtime", json={"speed_limit": 500_000}).json()["max_speed"] == 1_000_000
        assert post(c, speed_limit=5_000_000).status_code == 422  # a download asking for more
        assert effective_speed(None) == 1_000_000 and effective_speed(300) == 300 and effective_speed(9e9) == 1_000_000
        post(c)
        assert FakeYDL.last_opts["ratelimit"] <= 1_000_000  # what actually runs is held to the ceiling

    def test_no_ceiling_in_personal_mode(self, downloads_dir, monkeypatch):
        from services.ytdl_opts import effective_speed
        monkeypatch.setattr(get_settings().server, "max_speed", 1000)
        assert effective_speed(None) is None


class TestLiveConfig:
    @pytest.fixture
    def cfg(self, server, monkeypatch):
        from services.admin_config import AdminConfigService
        svc = AdminConfigService()
        yield svc
        svc.update({}, reset=[k["key"] for k in svc.list()])  # restore the base values on the shared settings

    def test_list_update_reset(self, cfg):
        before = {k["key"]: k for k in cfg.list()}
        assert before["server.delivery_ttl_minutes"]["value"] == 60 and not before["server.delivery_ttl_minutes"]["overridden"]
        after = {k["key"]: k for k in cfg.update({"server.delivery_ttl_minutes": 15, "server.max_speed": "2M"})}
        assert get_settings().server.delivery_ttl_minutes == 15 and get_settings().server.max_speed == 2 * 1024 * 1024
        assert after["server.delivery_ttl_minutes"]["overridden"] and after["server.delivery_ttl_minutes"]["default"] == 60
        cfg.update({}, reset=["server.delivery_ttl_minutes"])
        assert get_settings().server.delivery_ttl_minutes == 60

    def test_all_or_nothing(self, cfg):
        from services.errors import InvalidRequestError
        with pytest.raises(InvalidRequestError, match="Keep finished files"):
            cfg.update({"server.max_job_items": 7, "server.delivery_ttl_minutes": 0})
        assert get_settings().server.max_job_items == 100  # nothing was written

    def test_parallel_cap_cannot_exceed_the_threads(self, cfg):
        from services.errors import InvalidRequestError
        with pytest.raises(InvalidRequestError):
            cfg.update({"task.max_parallel_cap": get_settings().task.workers + 1})

    def test_other_process_picks_it_up(self, cfg):
        from services.admin_config import AdminConfigService
        other = AdminConfigService()  # e.g. the worker
        other.apply(force=True)
        cfg.update({"download.network_retries": 9})
        assert other.apply() is True and get_settings().download.network_retries == 9
        assert other.apply() is False  # nothing new

    def test_unknown_key(self, cfg):
        from services.errors import InvalidRequestError
        with pytest.raises(InvalidRequestError, match="Unknown"):
            cfg.update({"redis.redis_url": "x"})


def test_server_parallel_ceiling_is_the_worker_threads(app, monkeypatch):
    monkeypatch.setattr(get_settings().task, "max_parallel_cap", 12)
    monkeypatch.setattr(get_settings().task, "workers", 6)
    c = visitor(app)
    c.post("/api/admin/login", json={"username": "admin", "password": PASSWORD})
    assert c.get("/api/runtime").json()["max_parallel_cap"] == 6
    assert c.put("/api/runtime", json={"max_parallel": 7}).status_code == 422
