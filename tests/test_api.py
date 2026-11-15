"""api/ — every endpoint through FastAPI's TestClient.

Huey runs in immediate mode (TASK_IMMEDIATE=true) with a scripted yt-dlp, so a
POST /api/downloads really executes the whole pipeline inline.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient

import api.routers.events as events_mod
import services.downloader as dl_mod
from api.main import create_app
from models import EntryInfo
from services import download_manager, metadata_service
from services import job_store as store
from services.metadata import PlaylistInfo
from tasks import heartbeat_key
from tests.fakes import FakeYDL, make_download_error

VID = "dQw4w9WgXcQ"
PL = "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"
WATCH = f"https://www.youtube.com/watch?v={VID}"
PLAYLIST = f"https://www.youtube.com/playlist?list={PL}"


@pytest.fixture
def client(downloads_dir, fake_ydl, monkeypatch):
    monkeypatch.setattr(dl_mod, "_CHECK_INTERVAL", 0.0)
    return TestClient(create_app())  # no `with`: lifespan (real Redis check) not entered


def post(client, urls=None, **kw):
    return client.post("/api/downloads", json={"urls": urls or [WATCH], **kw})


# ───────────────────────── system ─────────────────────────


class TestSystem:
    def test_health_degraded_without_worker(self, client):
        r = client.get("/api/health").json()
        assert r["redis"] is True and r["worker"] is False and r["status"] == "degraded"
        assert any("consumer" in d for d in r["detail"])
        assert r["ffmpeg"] and r["yt_dlp"] and r["downloads_dir"]

    def test_health_ok_with_worker_heartbeat(self, client, fake_redis):
        fake_redis.set(heartbeat_key(), str(time.time()), ex=60)
        r = client.get("/api/health").json()
        assert r["worker"] is True and r["status"] == "ok"

    def test_health_reports_redis_outage(self, client, monkeypatch):
        class Dead:
            def ping(self): raise redis_lib.exceptions.ConnectionError("refused")
            def exists(self, *a): raise redis_lib.exceptions.ConnectionError("refused")

        import api.routers.system as sysmod

        monkeypatch.setattr(sysmod, "get_redis", lambda: Dead())
        r = client.get("/api/health").json()
        assert r["redis"] is False and r["status"] == "degraded" and any("redis" in d for d in r["detail"])

    def test_config(self, client):
        c = client.get("/api/config").json()
        assert {"mp3", "m4a", "opus", "flac", "wav"} == set(c["audio_formats"])
        assert {"mp4", "mkv", "webm"} == set(c["video_formats"])
        assert c["audio_bitrates"][-1] == "320" and c["video_heights"][-1] == "2160"
        assert c["default_format"] == "mp3" and c["max_parallel_cap"] >= 1 and c["workers"] >= 1

    def test_runtime_get_put(self, client):
        base = client.get("/api/runtime").json()
        assert base["max_parallel"] >= 1
        r = client.put("/api/runtime", json={"max_parallel": 2}).json()
        assert r["max_parallel"] == 2
        r = client.put("/api/runtime", json={"speed_limit": 500000}).json()
        assert r == {"max_parallel": 2, "speed_limit": 500000, "default_folder": None,  # earlier change kept
                     "max_parallel_cap": 12, "max_speed": None}  # ceilings are reported  # ceilings are reported (personal mode: no speed ceiling)
        r = client.put("/api/runtime", json={"speed_limit": None}).json()
        assert r["speed_limit"] is None and r["max_parallel"] == 2
        assert client.put("/api/runtime", json={}).json()["max_parallel"] == 2  # empty body = no-op

    def test_runtime_clamped_to_cap(self, client):
        cap = client.get("/api/config").json()["max_parallel_cap"]
        assert client.put("/api/runtime", json={"max_parallel": 64}).json()["max_parallel"] == cap

    @pytest.mark.parametrize("body", [{"max_parallel": 0}, {"max_parallel": 999}, {"speed_limit": -1}, {"max_parallel": "x"}])
    def test_runtime_validation(self, client, body):
        assert client.put("/api/runtime", json=body).status_code == 422

    def test_docs_and_openapi(self, client):
        assert client.get("/docs").status_code == 200
        paths = client.get("/openapi.json").json()["paths"]
        for p in ("/api/downloads", "/api/downloads/{job_id}", "/api/items/{item_id}/{action}",
                  "/api/queue/{action}", "/api/info", "/api/events", "/api/library", "/api/health"):
            assert p in paths

    def test_cors_headers(self, client):
        r = client.get("/api/config", headers={"Origin": "http://example.com"})
        assert r.headers["access-control-allow-origin"] in ("*", "http://example.com")

    def test_static_assets_are_served_with_revalidation(self, client):
        r = client.get("/static/css/app.css")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.headers.get("etag")
        assert client.get("/static/js/app.js").status_code == 200
        assert client.get("/static/css/app.css", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
        assert client.get("/static/../pyproject.toml").status_code == 404  # no path traversal out of /static

    def test_index_serves_the_ui(self, client):
        r = client.get("/")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "mst-ytdl" in r.text
        assert r.headers["cache-control"] == "no-cache"

    def test_index_without_ui_is_a_friendly_json(self, client, monkeypatch, tmp_path):
        import api.main as m

        monkeypatch.setattr(m, "WEB_DIR", tmp_path)
        r = TestClient(m.create_app()).get("/")
        assert r.status_code == 200 and "docs" in r.json()["detail"]


# ───────────────────────── info ─────────────────────────


class TestInfo:
    def test_preview_single(self, client, monkeypatch):
        monkeypatch.setattr(metadata_service, "_extract", lambda url, flat: {
            "id": VID, "title": "Song", "uploader": "Artist", "duration": 100,
            "formats": [{"height": 720, "vcodec": "avc1"}, {"height": 360, "vcodec": "avc1"}]})
        r = client.post("/api/info", json={"url": WATCH})
        assert r.status_code == 200
        j = r.json()
        assert j["kind"] == "single" and j["title"] == "Song" and j["available_heights"] == [360, 720]

    def test_preview_playlist_with_mode(self, client, monkeypatch):
        monkeypatch.setattr(metadata_service, "_extract", lambda url, flat: {
            "title": "Mix", "entries": [{"id": "a" * 11, "title": "One"}, {"id": "b" * 11, "title": "Two"}]})
        j = client.post("/api/info", json={"url": f"{WATCH}&list={PL}", "playlist_mode": "playlist"}).json()
        assert j["kind"] == "playlist" and j["entry_count"] == 2 and j["ambiguous"] is True

    @pytest.mark.parametrize("url", ["https://vimeo.com/1", "nonsense", "https://youtube.com/watch?v=short"])
    def test_bad_url_is_422_with_reason(self, client, url):
        r = client.post("/api/info", json={"url": url})
        assert r.status_code == 422 and r.json()["detail"]

    def test_yt_dlp_error_is_422(self, client, monkeypatch):
        def boom(url, flat):
            raise make_download_error("[youtube] abcdefghijk: Video unavailable")

        monkeypatch.setattr(metadata_service, "_extract", boom)
        r = client.post("/api/info", json={"url": WATCH})
        assert r.status_code == 422 and "Video unavailable" in r.json()["detail"]

    @pytest.mark.parametrize("body", [{}, {"url": ""}, {"url": "x", "playlist_mode": "nope"}])
    def test_schema_validation(self, client, body):
        assert client.post("/api/info", json=body).status_code == 422


class TestSearchApi:
    def test_search(self, client, monkeypatch):
        monkeypatch.setattr(metadata_service, "_extract", lambda url, flat: {"entries": [
            {"id": "a" * 11, "title": "One", "channel": "C", "duration": 10}]})
        r = client.get("/api/search", params={"q": "lofi", "limit": 3})
        assert r.status_code == 200
        (e,) = r.json()
        assert e["title"] == "One" and e["uploader"] == "C" and e["url"].endswith("a" * 11)

    def test_music_param(self, client, monkeypatch):
        monkeypatch.setattr(metadata_service, "_extract", lambda url, flat: {"entries": [{"id": "a" * 11, "title": "One"}]})
        assert client.get("/api/search", params={"q": "lofi", "music": "true"}).json()[0]["url"].startswith("https://music.")

    @pytest.mark.parametrize("params", [{}, {"q": "a"}, {"q": "x" * 201}, {"q": "ok", "limit": 0}, {"q": "ok", "limit": 26}])
    def test_validation(self, client, params):
        assert client.get("/api/search", params=params).status_code == 422

    def test_upstream_error_is_422(self, client, monkeypatch):
        def boom(url, flat):
            raise make_download_error("rate limited")

        monkeypatch.setattr(metadata_service, "_extract", boom)
        assert client.get("/api/search", params={"q": "lofi"}).status_code == 422


# ───────────────────────── downloads ─────────────────────────


class TestCreate:
    def test_single_runs_end_to_end(self, client, downloads_dir):
        r = post(client, format="mp3", quality="320")
        assert r.status_code == 201
        job = r.json()["jobs"][0]
        assert job["status"] == "completed" and job["percent"] == 100.0
        assert job["kind"] == "single" and job["options"]["quality"] == "320"
        (item,) = job["items"]
        assert item["status"] == "completed" and item["title"] == "Fake Song"
        assert (downloads_dir / item["filepath"]).is_file()
        assert item["filepath"].endswith("/Fake Song.mp3")

    def test_mp4_with_quality(self, client, downloads_dir):
        j = post(client, format="mp4", quality="720").json()["jobs"][0]
        assert j["items"][0]["filepath"].endswith(".mp4")
        assert FakeYDL.last_opts["merge_output_format"] == "mp4"

    def test_rejected_urls_reported_alongside_created_jobs(self, client):
        r = post(client, [WATCH, "https://vimeo.com/9"])
        assert r.status_code == 201
        j = r.json()
        assert len(j["jobs"]) == 1 and j["rejected"][0]["url"] == "https://vimeo.com/9"

    def test_all_rejected_is_422(self, client):
        r = post(client, ["https://vimeo.com/9"])
        assert r.status_code == 422

    @pytest.mark.parametrize("body", [
        {"urls": []}, {"urls": [""]}, {"format": "mp3"}, {"urls": [WATCH], "format": "exe"},
        {"urls": [WATCH], "playlist_mode": "x"}, {"urls": [WATCH], "speed_limit": 0},
    ])
    def test_schema_validation(self, client, body):
        assert client.post("/api/downloads", json=body).status_code == 422

    @pytest.mark.parametrize("fmt,q", [("mp3", "1080"), ("mp4", "320")])
    def test_option_mismatch_is_422(self, client, fmt, q):
        r = post(client, format=fmt, quality=q)
        assert r.status_code == 422 and "quality" in r.json()["detail"].lower()

    def test_failure_is_surfaced_in_the_job(self, client):
        FakeYDL.fail_with = make_download_error("Private video")
        j = post(client).json()["jobs"][0]
        assert j["status"] == "failed" and "Private video" in j["items"][0]["error"]

    def test_playlist_end_to_end(self, client, downloads_dir, monkeypatch):
        entries = [EntryInfo(index=i, video_id=f"{i:011d}", url=f"https://www.youtube.com/watch?v={i:011d}", title=f"T{i}")
                   for i in range(1, 4)]
        monkeypatch.setattr(metadata_service, "expand_playlist", lambda u: PlaylistInfo("Road Trip", "DJ", None, entries))
        j = post(client, [PLAYLIST]).json()["jobs"][0]
        full = client.get(f"/api/downloads/{j['job_id']}").json()
        assert full["status"] == "completed" and full["folder"] == "Road Trip" and full["title"] == "Road Trip"
        assert [i["status"] for i in full["items"]] == ["completed"] * 3
        files = sorted(p.name for p in (downloads_dir / "Road Trip").iterdir())
        assert files == ["01 - Fake Song.mp3", "02 - Fake Song.mp3", "03 - Fake Song.mp3"]

    def test_playlist_selection(self, client, downloads_dir, monkeypatch):
        entries = [EntryInfo(index=i, video_id=f"{i:011d}", url=f"https://www.youtube.com/watch?v={i:011d}", title=f"T{i}")
                   for i in range(1, 6)]
        monkeypatch.setattr(metadata_service, "expand_playlist", lambda u: PlaylistInfo("Pick", None, None, entries))
        jid = post(client, [PLAYLIST], playlist_items="2,4-5").json()["jobs"][0]["job_id"]
        full = client.get(f"/api/downloads/{jid}").json()
        assert [i["index"] for i in full["items"]] == [2, 4, 5]
        assert sorted(p.name[:2] for p in (downloads_dir / "Pick").iterdir()) == ["02", "04", "05"]


class TestReadAndControl:
    def seed(self, client, **kw):
        return post(client, **kw).json()["jobs"][0]

    def test_list_get_and_404(self, client):
        a, b = self.seed(client), self.seed(client)
        lst = client.get("/api/downloads").json()
        assert [j["job_id"] for j in lst] == [b["job_id"], a["job_id"]]
        assert all(j["items"] is None for j in lst)
        assert client.get("/api/downloads?include_items=true").json()[0]["items"]
        assert client.get(f"/api/downloads/{a['job_id']}").json()["job_id"] == a["job_id"]
        assert client.get("/api/downloads/ghost").status_code == 404

    def test_list_filters_and_pagination(self, client):
        self.seed(client)
        FakeYDL.fail_with = make_download_error("Private video")
        failed = self.seed(client, overwrite=True)  # same title as above → would be skipped as a duplicate
        assert [j["job_id"] for j in client.get("/api/downloads?status=failed").json()] == [failed["job_id"]]
        assert len(client.get("/api/downloads?status=completed").json()) == 1
        assert len(client.get("/api/downloads?limit=1").json()) == 1
        assert len(client.get("/api/downloads?limit=1&offset=1").json()) == 1
        for bad in ("status=bogus", "limit=0", "limit=9999", "offset=-1"):
            assert client.get(f"/api/downloads?{bad}").status_code == 422

    def test_item_get(self, client):
        job = self.seed(client)
        iid = job["items"][0]["item_id"]
        assert client.get(f"/api/items/{iid}").json()["status"] == "completed"
        assert client.get("/api/items/ghost").status_code == 404

    def test_item_actions_and_conflicts(self, client):
        iid = self.seed(client)["items"][0]["item_id"]
        for action in ("pause", "resume", "cancel"):
            r = client.post(f"/api/items/{iid}/{action}")
            assert r.status_code == 409 and r.json()["detail"], action  # completed items can't be controlled
        assert client.post(f"/api/items/{iid}/explode").status_code == 422
        assert client.post("/api/items/ghost/pause").status_code == 404

    def test_retry_failed_item_over_http(self, client):
        FakeYDL.fail_with = make_download_error("Private video")
        job = self.seed(client)
        iid = job["items"][0]["item_id"]
        FakeYDL.fail_with = None
        r = client.post(f"/api/items/{iid}/retry")
        assert r.status_code == 200
        assert client.get(f"/api/items/{iid}").json()["status"] == "completed"  # re-ran inline

    def test_job_actions(self, client):
        job = self.seed(client)
        jid = job["job_id"]
        for action in ("pause", "resume", "cancel", "retry"):
            r = client.post(f"/api/downloads/{jid}/{action}")
            assert r.status_code == 200 and r.json()["job_id"] == jid  # no eligible items: harmless no-op
        assert client.post("/api/downloads/ghost/pause").status_code == 404

    def test_pause_queued_then_resume_over_http(self, client, dispatcher):
        job = post(client).json()["jobs"][0]  # fake dispatcher: nothing runs
        iid = job["items"][0]["item_id"]
        assert client.post(f"/api/items/{iid}/pause").json()["status"] == "paused"
        assert client.post(f"/api/items/{iid}/resume").json()["status"] == "queued"
        assert client.post(f"/api/items/{iid}/cancel").json()["status"] == "cancelled"
        assert client.post(f"/api/items/{iid}/retry").json()["status"] == "queued"

    def test_delete_job(self, client, downloads_dir):
        job = self.seed(client)
        path = downloads_dir / job["items"][0]["filepath"]
        r = client.delete(f"/api/downloads/{job['job_id']}")
        assert r.status_code == 200 and path.exists()
        assert client.get(f"/api/downloads/{job['job_id']}").status_code == 404
        assert client.delete("/api/downloads/ghost").status_code == 404

    def test_delete_job_with_files(self, client, downloads_dir):
        job = self.seed(client)
        path = downloads_dir / job["items"][0]["filepath"]
        r = client.delete(f"/api/downloads/{job['job_id']}?delete_files=true")
        assert "1 file" in r.json()["message"] and not path.exists()

    def test_queue_actions(self, client, dispatcher):
        ids = [post(client).json()["jobs"][0]["items"][0]["item_id"] for _ in range(3)]
        assert client.post("/api/queue/pause-all").json()["affected"] == 3
        assert client.post("/api/queue/resume-all").json()["affected"] == 3
        assert client.post("/api/queue/cancel-all").json()["affected"] == 3
        assert client.post("/api/queue/retry-failed").json()["affected"] == 0  # cancelled ≠ failed
        assert client.post("/api/queue/clear-finished").json()["affected"] == 3
        assert client.get("/api/downloads").json() == [] and ids
        assert client.post("/api/queue/nuke").status_code == 422

    def test_clear_finished_delete_files(self, client, downloads_dir):
        job = self.seed(client)
        path = downloads_dir / job["items"][0]["filepath"]
        assert client.post("/api/queue/clear-finished?delete_files=true").json()["affected"] == 1
        assert not path.exists()

    def test_redis_outage_maps_to_503(self, client, monkeypatch):
        def boom(**kw):
            raise redis_lib.exceptions.ConnectionError("connection refused")

        monkeypatch.setattr(download_manager, "list_job_views", boom)
        r = client.get("/api/downloads")
        assert r.status_code == 503 and "Redis unavailable" in r.json()["detail"]


# ───────────────────────── library ─────────────────────────


class TestLibraryApi:
    @pytest.fixture
    def seeded(self, client, downloads_dir):
        job = post(client).json()["jobs"][0]
        return job["items"][0]["filepath"], job["folder"]

    def test_listing(self, client, seeded):
        path, folder = seeded
        root = client.get("/api/library").json()
        assert [e["name"] for e in root] == [folder] and root[0]["is_dir"]
        inner = client.get("/api/library", params={"path": folder}).json()
        assert inner[0]["name"] == "Fake Song.mp3" and inner[0]["size"] == 1000
        assert client.get("/api/library", params={"path": "ghost"}).status_code == 404
        assert client.get("/api/library", params={"path": path}).status_code == 422  # a file, not a folder

    def test_usage(self, client, seeded):
        u = client.get("/api/library/usage").json()
        assert (u["files"], u["bytes"]) == (1, 1000)
        assert u["disk_total"] >= u["disk_used"] > 0 and u["disk_free"] > 0  # the storage bar

    def test_file_inline_and_download(self, client, seeded):
        path, _ = seeded
        r = client.get("/api/library/file", params={"path": path})
        assert r.status_code == 200 and len(r.content) == 1000 and "inline" in r.headers["content-disposition"]
        r = client.get("/api/library/file", params={"path": path, "download": "true"})
        from urllib.parse import unquote

        cd = unquote(r.headers["content-disposition"])  # RFC 5987: filename*=utf-8''Fake%20Song.mp3
        assert cd.startswith("attachment") and "Fake Song.mp3" in cd

    def test_file_supports_range(self, client, seeded):
        path, _ = seeded
        r = client.get("/api/library/file", params={"path": path}, headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and len(r.content) == 100
        assert r.headers["content-range"].startswith("bytes 0-99/")

    def test_file_errors(self, client, seeded):
        assert client.get("/api/library/file", params={"path": "ghost.mp3"}).status_code == 404
        assert client.get("/api/library/file", params={"path": "../../etc/passwd"}).status_code == 422
        assert client.get("/api/library/file").status_code == 422

    def test_zip(self, client, seeded):
        _, folder = seeded
        r = client.get("/api/library/zip", params={"path": folder})
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert f"{folder}.zip" in r.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            assert z.namelist() == ["Fake Song.mp3"] and z.testzip() is None
        assert client.get("/api/library/zip", params={"path": "ghost"}).status_code == 404
        assert client.get("/api/library/zip", params={"path": ".."}).status_code == 422

    def test_delete(self, client, seeded, downloads_dir):
        path, folder = seeded
        assert client.delete("/api/library", params={"path": path}).json()["affected"] == 1
        assert not (downloads_dir / path).exists()
        assert client.delete("/api/library", params={"path": path}).status_code == 404
        assert client.delete("/api/library", params={"path": ""}).status_code == 422  # root is protected
        assert client.delete("/api/library", params={"path": "../x"}).status_code == 422
        assert downloads_dir.exists()


# ───────────────────────── SSE ─────────────────────────


class FakeRequest:
    """Starlette-Request stand-in: connected for N polls, then disconnects."""

    def __init__(self, polls: int):
        self.polls = polls

    async def is_disconnected(self) -> bool:
        self.polls -= 1
        return self.polls < 0


def collect(polls: int, mutate=None):
    async def run():
        frames = []
        async for f in events_mod._stream(FakeRequest(polls)):
            frames.append(f)
            if mutate:
                mutate(len(frames))
        return frames

    return asyncio.run(run())


@pytest.fixture
def fast_sse(monkeypatch):
    monkeypatch.setattr(events_mod, "POLL_SECONDS", 0.01)


class TestSse:
    def test_first_frames_are_retry_hint_and_snapshot(self, fast_sse, dispatcher):
        download_manager.submit(__import__("models").DownloadRequest(urls=[WATCH]))
        frames = collect(2)
        assert frames[0] == "retry: 2000\n\n"
        assert frames[1].startswith("event: snapshot\ndata: ")
        payload = json.loads(frames[1].split("data: ", 1)[1])
        assert payload["stats"]["queued"] == 1 and payload["jobs"][0]["items"][0]["status"] == "queued"

    def test_identical_snapshots_are_suppressed(self, fast_sse, dispatcher):
        download_manager.submit(__import__("models").DownloadRequest(urls=[WATCH]))
        frames = collect(8)
        assert sum(f.startswith("event: snapshot") for f in frames) == 1

    def test_change_emits_a_new_snapshot(self, fast_sse, dispatcher):
        req = __import__("models").DownloadRequest(urls=[WATCH])
        download_manager.submit(req)
        iid = store.get_item_ids(store.list_job_ids()[0])[0]

        def mutate(n):
            if n == 2:  # after the first snapshot
                store.patch_item(iid, percent=50.0)

        frames = collect(8, mutate)
        snaps = [f for f in frames if f.startswith("event: snapshot")]
        assert len(snaps) == 2 and '"percent":50.0' in snaps[1]

    def test_keepalive_when_idle(self, fast_sse, monkeypatch):
        monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.0)
        frames = collect(6)
        assert ": keepalive\n\n" in frames

    def test_snapshot_failure_becomes_a_problem_event_and_stream_survives(self, fast_sse, monkeypatch):
        calls = {"n": 0}
        real = download_manager.snapshot

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("redis hiccup")
            return real()

        monkeypatch.setattr(download_manager, "snapshot", flaky)
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        frames = collect(6)
        assert any(f.startswith("event: problem") and "redis hiccup" in f for f in frames)
        assert any(f.startswith("event: snapshot") for f in frames)  # recovered

    def test_disconnect_ends_the_stream(self, fast_sse):
        assert collect(0) == ["retry: 2000\n\n"]

    def test_route_is_registered_as_event_stream(self, client):
        spec = client.get("/openapi.json").json()
        assert "/api/events" in spec["paths"]


_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *a, **k):
    await _real_sleep(0.001)
