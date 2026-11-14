"""New endpoints: restart, bulk jobs, library bulk-delete / move / rename / multi-zip / folders."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

import services.downloader as dl_mod
from api.main import create_app
from tests.fakes import FakeYDL, make_download_error

WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture
def client(downloads_dir, fake_ydl, monkeypatch):
    monkeypatch.setattr(dl_mod, "_CHECK_INTERVAL", 0.0)
    return TestClient(create_app())


def post(client, **kw):
    return client.post("/api/downloads", json={"urls": [WATCH], **kw}).json()["jobs"][0]


class TestRestartEndpoints:
    def test_restart_replaces_a_finished_download(self, client, downloads_dir):
        job = post(client)
        item = job["items"][0]
        f = downloads_dir / item["filepath"]
        f.write_bytes(b"tampered")
        r = client.post(f"/api/items/{item['item_id']}/restart")
        assert r.status_code == 200
        again = client.get(f"/api/items/{item['item_id']}").json()
        assert again["status"] == "completed" and again["force"] is False  # ran inline, flag spent
        assert f.read_bytes() != b"tampered"

    def test_restart_failed_job(self, client):
        FakeYDL.fail_with = make_download_error("Private video")
        job = post(client)
        assert job["status"] == "failed"
        FakeYDL.fail_with = None
        r = client.post(f"/api/downloads/{job['job_id']}/restart")
        assert r.status_code == 200 and r.json()["status"] == "completed"

    def test_restart_conflicts_and_404(self, client, dispatcher):
        job = post(client)  # fake dispatcher → stays queued
        iid = job["items"][0]["item_id"]
        assert client.post(f"/api/items/{iid}/restart").status_code == 409
        assert client.post("/api/items/ghost/restart").status_code == 404
        assert client.post("/api/downloads/ghost/restart").status_code == 404


class TestBulkDownloads:
    def make(self, client, n=3):
        return [post(client, overwrite=True, folder=f"f{i}")["job_id"] for i in range(n)]

    def test_actions_over_http(self, client, dispatcher):
        ids = self.make(client)
        r = client.post("/api/downloads/bulk", json={"ids": ids, "action": "pause"})
        assert r.status_code == 200 and r.json() == {"affected": 3, "skipped": 0, "errors": []}
        assert client.post("/api/downloads/bulk", json={"ids": ids, "action": "resume"}).json()["affected"] == 3
        assert client.post("/api/downloads/bulk", json={"ids": ids, "action": "cancel"}).json()["affected"] == 3
        r = client.post("/api/downloads/bulk", json={"ids": ids, "action": "remove"})
        assert r.json()["affected"] == 3 and client.get("/api/downloads").json() == []

    def test_unknown_ids_reported(self, client, dispatcher):
        ids = self.make(client, 1)
        r = client.post("/api/downloads/bulk", json={"ids": [*ids, "ghost"], "action": "pause"}).json()
        assert r["affected"] == 1 and r["errors"][0]["id"] == "ghost"

    def test_remove_with_files(self, client, downloads_dir):
        job = post(client)
        f = downloads_dir / job["items"][0]["filepath"]
        r = client.post("/api/downloads/bulk", json={"ids": [job["job_id"]], "action": "remove-files"})
        assert r.json()["affected"] == 1 and not f.exists()

    @pytest.mark.parametrize("body", [
        {"ids": [], "action": "pause"}, {"action": "pause"}, {"ids": ["a"], "action": "explode"},
        {"ids": ["a"]}, {"ids": [f"x{i}" for i in range(501)], "action": "pause"},
    ])
    def test_validation(self, client, body):
        assert client.post("/api/downloads/bulk", json=body).status_code == 422

    def test_route_does_not_shadow_job_lookup(self, client):
        assert client.get("/api/downloads/bulk").status_code == 404  # GET still means "job named bulk"


@pytest.fixture
def lib(client, downloads_dir):
    (downloads_dir / "Mix").mkdir()
    (downloads_dir / "Mix" / "a.mp3").write_bytes(b"a" * 10)
    (downloads_dir / "Mix" / "b.mp3").write_bytes(b"b" * 20)
    (downloads_dir / "loose.mp4").write_bytes(b"l" * 30)
    return client


class TestLibraryBulkApi:
    def test_folders(self, lib):
        assert lib.get("/api/library/folders").json() == ["Mix"]

    def test_multi_zip(self, lib):
        r = lib.get("/api/library/zip", params=[("path", "Mix/a.mp3"), ("path", "loose.mp4")])
        assert r.status_code == 200 and "selection.zip" in r.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            assert sorted(z.namelist()) == ["Mix/a.mp3", "loose.mp4"]

    def test_single_folder_zip_still_works(self, lib):
        r = lib.get("/api/library/zip", params={"path": "Mix"})
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            assert sorted(z.namelist()) == ["a.mp3", "b.mp3"]

    def test_zip_errors(self, lib):
        assert lib.get("/api/library/zip").status_code == 422
        assert lib.get("/api/library/zip", params=[("path", "Mix"), ("path", "ghost")]).status_code == 404
        assert lib.get("/api/library/zip", params=[("path", "Mix"), ("path", "../x")]).status_code == 422

    def test_bulk_delete(self, lib, downloads_dir):
        r = lib.post("/api/library/bulk-delete", json={"paths": ["loose.mp4", "Mix/a.mp3", "ghost"]}).json()
        assert r["deleted"] == 2 and r["files"] == 2 and r["errors"][0]["path"] == "ghost"
        assert not (downloads_dir / "loose.mp4").exists() and (downloads_dir / "Mix" / "b.mp3").exists()

    def test_bulk_delete_validation_and_root(self, lib, downloads_dir):
        assert lib.post("/api/library/bulk-delete", json={"paths": []}).status_code == 422
        r = lib.post("/api/library/bulk-delete", json={"paths": [""]}).json()
        assert r["deleted"] == 0 and r["errors"] and downloads_dir.exists()

    def test_move(self, lib, downloads_dir):
        r = lib.post("/api/library/move", json={"paths": ["loose.mp4"], "dest": "Archive/2026"}).json()
        assert r == {"moved": 1, "dest": "Archive/2026", "errors": []}
        assert (downloads_dir / "Archive" / "2026" / "loose.mp4").exists()
        assert lib.post("/api/library/move", json={"paths": ["x"]}).status_code == 422  # dest required

    def test_move_conflict_reported(self, lib):
        lib.post("/api/library/move", json={"paths": ["loose.mp4"], "dest": "Mix"})
        r =lib.post("/api/library/move", json={"paths": ["Mix/loose.mp4"], "dest": "Mix"}).json()
        assert r["moved"] == 0 and r["errors"] == []  # already there

    def test_rename(self, lib, downloads_dir):
        r = lib.post("/api/library/rename", json={"path": "loose.mp4", "name": "Holiday"})
        assert r.status_code == 200 and r.json()["name"] == "Holiday.mp4"
        assert (downloads_dir / "Holiday.mp4").exists()

    def test_rename_errors(self, lib):
        assert lib.post("/api/library/rename", json={"path": "ghost", "name": "x"}).status_code == 404
        assert lib.post("/api/library/rename", json={"path": "Mix/a.mp3", "name": "b"}).status_code == 409
        assert lib.post("/api/library/rename", json={"path": "", "name": "x"}).status_code == 422
        assert lib.post("/api/library/rename", json={"path": "loose.mp4", "name": ""}).status_code == 422
        assert lib.post("/api/library/rename", json={"path": "../x", "name": "y"}).status_code == 422
