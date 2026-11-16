"""services/webhook.py — one POST per finished job (DL_WEBHOOK_URL), both modes, never raises."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from configs import get_settings
from models import DownloadItem, DownloadJob, DownloadOptions, JobKind, MediaFormat
from models import DownloadStatus as S
from services import download_manager as mgr
from services import download_runner as runner
from services import job_store as store
from services import webhook_service
from services.webhook import _header_text
from tests.fakes import FakeYDL, make_download_error

URL = "https://www.youtube.com/watch?v=abc123DEF45"


def make(n=1, kind=JobKind.SINGLE, **jobkw):
    """A job with ``n`` queued items; returns [(item_id, token)]."""
    store.create_job(DownloadJob(job_id="job1", url=URL, kind=kind, folder="2026-01-01", title="My Job",
                                 options=DownloadOptions(format=MediaFormat.MP3), total_items=n, **jobkw))
    items = [DownloadItem(item_id=f"it{i}", job_id="job1", index=i, url=URL, token=f"tok{i}") for i in range(1, n + 1)]
    store.add_items(items)
    return [(i.item_id, i.token) for i in items]


@pytest.fixture
def sent(monkeypatch, downloads_dir, fake_ydl, dispatcher):
    """Webhook enabled; requests are recorded instead of sent."""
    monkeypatch.setattr(get_settings().download, "webhook_url", "http://hook.test/x")
    out: list[tuple[str, dict]] = []
    monkeypatch.setattr(webhook_service, "_dispatch", lambda url, payload: out.append((url, payload)))
    return out


class TestFiring:
    def test_off_by_default(self, monkeypatch, downloads_dir, fake_ydl, dispatcher):
        monkeypatch.setattr(get_settings().download, "webhook_url", None)
        monkeypatch.setattr(webhook_service, "_dispatch", lambda *a: pytest.fail("sent while disabled"))
        (iid, tok), = make()
        assert runner.run(iid, tok) == "completed"
        assert webhook_service.notify_job("job1") is False

    def test_completed_job_posts_once(self, sent):
        (iid, tok), = make()
        runner.run(iid, tok)
        assert len(sent) == 1
        url, body = sent[0]
        assert url == "http://hook.test/x"
        assert body["event"] == "job.completed" and body["status"] == "completed"
        assert body["job_id"] == "job1" and body["title"]  # the runner fills the title from yt-dlp
        assert body["kind"] == "single" and body["total_items"] == 1 and body["completed"] == 1 and body["failed"] == 0
        assert body["finished_at"] > 0
        json.dumps(body)  # serialisable

    def test_failed_job(self, sent):
        FakeYDL.fail_with = make_download_error("Private video")
        (iid, tok), = make()
        assert runner.run(iid, tok) == "failed"
        assert [b["status"] for _, b in sent] == ["failed"]
        assert sent[0][1]["failed"] == 1

    def test_playlist_fires_only_after_the_last_item(self, sent):
        (a, ta), (b, tb) = make(2, JobKind.PLAYLIST)
        runner.run(a, ta)
        assert sent == []  # item 2 is still queued
        runner.run(b, tb)
        assert len(sent) == 1 and sent[0][1]["completed"] == 2

    def test_partial_job(self, sent):
        (a, ta), (b, tb) = make(2, JobKind.PLAYLIST)
        runner.run(a, ta)
        FakeYDL.fail_with = make_download_error("Private video")
        runner.run(b, tb)
        assert [x["status"] for _, x in sent] == ["partial"]
        assert sent[0][1]["event"] == "job.partial"

    def test_cancelling_the_last_queued_item_fires(self, sent):
        (iid, _), = make()
        mgr.cancel_item(iid)
        assert [b["status"] for _, b in sent] == ["cancelled"]

    def test_a_still_active_job_does_not_fire(self, sent):
        make(2, JobKind.PLAYLIST)
        assert webhook_service.notify_job("job1") is False and sent == []

    def test_same_finish_is_announced_once(self, sent):
        (iid, tok), = make()
        runner.run(iid, tok)
        assert webhook_service.notify_job("job1") is False
        assert len(sent) == 1

    def test_a_retry_that_finishes_again_is_announced_again(self, sent):
        FakeYDL.fail_with = make_download_error("Private video")
        (iid, tok), = make()
        runner.run(iid, tok)
        FakeYDL.fail_with = None
        mgr.retry_item(iid)
        runner.run(iid, store.get_item(iid).token)
        assert [b["status"] for _, b in sent] == ["failed", "completed"]

    def test_failed_playlist_expansion_fires(self, sent, monkeypatch):
        from services import metadata_service

        store.create_job(DownloadJob(job_id="job1", url=URL, kind=JobKind.PLAYLIST, folder="x", title="PL",
                                     options=DownloadOptions(format=MediaFormat.MP3), total_items=0, resolve_state="pending"))
        monkeypatch.setattr(metadata_service, "expand_playlist",
                            lambda url: (_ for _ in ()).throw(make_download_error("Private playlist")))
        mgr.resolve_job("job1")
        assert [b["status"] for _, b in sent] == ["failed"] and sent[0][1]["error"]

    def test_a_broken_store_never_raises(self, sent, monkeypatch):
        monkeypatch.setattr(store, "get_job", lambda jid: (_ for _ in ()).throw(RuntimeError("redis down")))
        assert webhook_service.notify_job("job1") is False


class TestSending:
    @pytest.fixture
    def server(self):
        got: list[dict] = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                got.append({"body": json.loads(body), "title": self.headers["Title"], "tags": self.headers["Tags"],
                            "ctype": self.headers["Content-Type"]})
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield f"http://127.0.0.1:{srv.server_port}/topic", got
        srv.shutdown()
        srv.server_close()

    def test_post_sends_json_and_ntfy_headers(self, server):
        url, got = server
        payload = {"event": "job.completed", "status": "completed", "title": "Café – Song", "job_id": "j"}
        assert webhook_service._post(url, payload) is True
        assert got[0]["body"] == payload and got[0]["ctype"] == "application/json"
        assert got[0]["tags"] == "white_check_mark" and got[0]["title"].startswith("=?UTF-8?B?")

    def test_unreachable_endpoint_returns_false(self):
        assert webhook_service._post("http://127.0.0.1:1/x", {"status": "failed", "title": "t"}) is False

    def test_bad_payload_returns_false(self):
        assert webhook_service._post("http://127.0.0.1:1/x", {}) is False

    def test_dispatch_is_asynchronous(self, monkeypatch):
        done = threading.Event()
        monkeypatch.setattr(webhook_service, "_post", lambda url, payload: done.set())
        webhook_service._dispatch("http://x", {"status": "failed"})
        assert done.wait(2)

    @pytest.mark.parametrize("raw,want", [("Plain", "Plain"), ("a\nb\r\nc", "a b c"), ("", "mst-ytdl")])
    def test_header_text_is_single_line(self, raw, want):
        assert _header_text(raw) == want

    def test_header_text_is_truncated(self):
        assert len(_header_text("x" * 500)) == 120


class TestStatusEnumCoverage:
    def test_every_terminal_status_has_a_tag(self):
        from services.webhook import _TAGS

        assert {s for s in S if s.is_terminal} == set(_TAGS)


def test_blank_env_value_is_off(monkeypatch):
    from configs.config import DownloadSettings

    monkeypatch.setenv("DL_WEBHOOK_URL", "  ")
    assert DownloadSettings().webhook_url is None
