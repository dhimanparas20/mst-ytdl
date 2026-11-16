"""Subscriptions: follow a channel / playlist, seed what exists, queue only new uploads.

yt-dlp is replaced at ``MetadataService._extract_recent`` (the one flat call); everything above it —
parsing, seeding, dedupe, options snapshot, owner scoping, the real ``download_manager.submit`` —
is the production code, with Redis from fakeredis and a recording dispatcher.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import services.subscriptions as sub_mod
from api.main import create_app
from configs import get_settings
from models import Subscription, SubscriptionRequest, SubscriptionView
from services import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    RateLimitedError,
    job_store,
    metadata_service,
    subscription_service,
)
from services.access import VISITOR_COOKIE
from services.metadata import is_short_entry
from tasks import poll_subscriptions_task

CHANNEL = "https://www.youtube.com/@somechannel"
PLAYLIST = "https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"
WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def vid(n: int) -> str:
    return f"vid{n:08d}"  # 11 characters, like a real id


def raw(n: int, *, duration: float | None = 300, title: str | None = None, url: str | None = None,
        live_status: str | None = None) -> dict:
    """A flat yt-dlp entry."""
    e = {"id": vid(n), "title": title or f"Video {n}", "duration": duration,
         "url": url or f"https://www.youtube.com/watch?v={vid(n)}"}
    if live_status:
        e["live_status"] = live_status
    return e


class Feed:
    """Scripted channel / playlist head. ``entries`` are newest first, like a channel's Videos tab."""

    def __init__(self) -> None:
        self.title = "Some Channel - Videos"
        self.entries: list[dict | None] = []
        self.calls: list[tuple[str, int]] = []
        self.error: BaseException | None = None
        self.on_call = None

    def set(self, *numbers: int, **kw) -> None:
        self.entries = [raw(n, **kw) for n in numbers]

    def __call__(self, url: str, limit: int) -> dict:
        self.calls.append((url, limit))
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        return {"title": self.title, "uploader": "Some Channel", "entries": self.entries[:limit]}


@pytest.fixture
def feed(monkeypatch) -> Feed:
    f = Feed()
    monkeypatch.setattr(metadata_service, "_extract_recent", f)
    return f


@pytest.fixture
def svc(feed, dispatcher):
    return subscription_service


def req(url: str = CHANNEL, **kw) -> SubscriptionRequest:
    return SubscriptionRequest(url=url, **kw)


def queued_urls(dispatcher) -> list[str]:
    return [job_store.get_item(item_id).url for item_id, _, _ in dispatcher.items]


def jobs(owner: str | None = None):
    """Job records (newest first), all or one visitor's."""
    return job_store.get_jobs(job_store.list_job_ids(0, 100, owner))


def queued_ids(dispatcher) -> list[str]:
    return [u.rsplit("=", 1)[1] for u in queued_urls(dispatcher)]


@pytest.fixture
def server(monkeypatch, downloads_dir):
    s = get_settings().server
    monkeypatch.setattr(s, "mode", True)
    monkeypatch.setattr(s, "min_free_mb", 0)
    monkeypatch.setattr(s, "max_active_jobs", 50)
    monkeypatch.setattr(s, "rate_limit_per_minute", 1000)
    monkeypatch.setattr(s, "max_subscriptions", 20)
    return s


# ───────────────────────── create / seed ─────────────────────────


class TestCreate:
    def test_channel_is_normalised_and_current_videos_are_seeded_not_downloaded(self, svc, feed, dispatcher):
        feed.set(3, 2, 1)
        sub = svc.create(req(format="mp4", quality="720p", sponsorblock=True, embed_thumbnail=False))
        assert sub.kind.value == "channel"
        assert sub.url == f"{CHANNEL}/videos"
        assert feed.calls == [(f"{CHANNEL}/videos", sub_mod.CHANNEL_SCAN)]
        assert sub.title == "Some Channel"  # the " - Videos" tab suffix is dropped
        assert sub.enabled and sub.skip_shorts and sub.last_check and sub.last_error is None
        assert sub.seen_count == 3
        assert svc.r.smembers(svc.seen_key(sub.sub_id)) == {vid(1), vid(2), vid(3)}
        assert dispatcher.items == [] and svc.get(sub.sub_id).downloaded == 0  # nothing downloaded at subscribe time
        # the options are a snapshot of the request
        o = sub.options
        assert (o.format.value, o.quality, o.sponsorblock, o.embed_thumbnail) == ("mp4", "720", True, False)

    def test_playlist_reads_the_whole_list(self, svc, feed):
        feed.title = "Road Trip"
        feed.set(1, 2)
        sub = svc.create(req(PLAYLIST))
        assert sub.kind.value == "playlist" and sub.title == "Road Trip"
        assert feed.calls[0][1] == get_settings().download.max_playlist_items

    def test_watch_url_with_list_follows_the_list(self, svc, feed):
        sub = svc.create(req(f"{WATCH}&list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"))
        assert sub.kind.value == "playlist" and sub.url == PLAYLIST

    def test_empty_playlist_is_fine(self, svc, feed):
        assert svc.create(req(PLAYLIST)).seen_count == 0

    @pytest.mark.parametrize("url", [WATCH, "https://example.com/@x", "not a url", f"{WATCH}&list=RDdQw4w9WgXcQ"])
    def test_not_followable_urls_are_refused(self, svc, feed, url):
        with pytest.raises(InvalidRequestError):
            svc.create(req(url))
        assert svc.list_for(None) == [] and feed.calls == []

    def test_same_url_twice_conflicts(self, svc, feed):
        svc.create(req())
        with pytest.raises(ConflictError):
            svc.create(req("https://youtube.com/@somechannel/videos"))  # same canonical URL

    def test_unreadable_source_stores_nothing(self, svc, feed):
        feed.error = RuntimeError("boom")
        with pytest.raises(InvalidRequestError):
            svc.create(req())
        assert svc.list_for(None) == [] and svc.r.zcard(svc.index) == 0

    def test_bad_options_are_refused(self, svc, feed):
        with pytest.raises(InvalidRequestError):
            svc.create(req(format="mp3", quality="999"))

    def test_personal_mode_cap(self, svc, feed, monkeypatch):
        monkeypatch.setattr(sub_mod, "PERSONAL_MAX", 1)
        svc.create(req())
        with pytest.raises(RateLimitedError):
            svc.create(req(PLAYLIST))

    def test_server_mode_cap_is_per_visitor(self, svc, feed, server):
        server.max_subscriptions = 1
        svc.create(req(), owner="alice")
        with pytest.raises(RateLimitedError):
            svc.create(req(PLAYLIST), owner="alice")
        svc.create(req(), owner="bob")  # bob has his own allowance
        assert svc.limit() == 1

    def test_server_mode_refuses_speed_above_the_ceiling(self, svc, feed, server, monkeypatch):
        monkeypatch.setattr(server, "max_speed", 1_000_000)
        with pytest.raises(InvalidRequestError):
            svc.create(req(speed_limit=5_000_000), owner="alice")

    def test_view_hides_the_owner(self, svc, feed, server):
        sub = svc.create(req(), owner="alice")
        assert sub.owner == "alice"
        assert "owner" not in SubscriptionView.of(sub).model_dump()


# ───────────────────────── polling ─────────────────────────


class TestPoll:
    def test_only_new_videos_are_queued_oldest_first(self, svc, feed, dispatcher):
        feed.set(3, 2, 1)
        sub = svc.create(req())
        feed.set(6, 5, 4, 3, 2, 1)  # three uploads since
        assert svc.poll_all() == {"checked": 1, "queued": 3}
        assert queued_ids(dispatcher) == [vid(4), vid(5), vid(6)]
        again = svc.get(sub.sub_id)
        assert (again.last_new, again.downloaded, again.last_error, again.seen_count) == (3, 3, None, 6)

        assert svc.poll_all() == {"checked": 1, "queued": 0}  # dedupe: nothing twice
        assert len(dispatcher.items) == 3
        assert svc.get(sub.sub_id).last_new == 0 and svc.get(sub.sub_id).downloaded == 3

    def test_queued_jobs_use_the_snapshot_and_a_channel_folder(self, svc, feed, dispatcher):
        feed.set(1)
        svc.create(req(format="mp4", quality="480", subtitles=True))
        feed.set(2, 1)
        svc.poll_all()
        (job,) = jobs()
        assert job.kind.value == "single" and job.url.endswith(vid(2))
        assert job.options.format.value == "mp4" and job.options.quality == "480" and job.options.subtitles
        assert job.folder == "Some Channel"
        assert job.options.overwrite is False

    def test_playlist_picks_up_appended_videos(self, svc, feed, dispatcher):
        feed.set(1, 2)  # a playlist lists oldest first: new ones are appended
        svc.create(req(PLAYLIST))
        feed.set(1, 2, 3)
        svc.poll_all()
        assert queued_ids(dispatcher) == [vid(3)]

    def test_shorts_are_skipped_but_remembered(self, svc, feed, dispatcher):
        sub = svc.create(req())
        feed.entries = [
            raw(4), raw(3, duration=45), raw(2, url=f"https://www.youtube.com/shorts/{vid(2)}"),
            raw(1, title="Fun #Shorts"),
        ]
        svc.poll_all()
        assert queued_ids(dispatcher) == [vid(4)]
        assert svc.get(sub.sub_id).seen_count == 4  # the shorts are known, so turning the filter off later is harmless

    def test_shorts_are_downloaded_when_the_filter_is_off(self, svc, feed, dispatcher):
        svc.create(req(skip_shorts=False))
        feed.entries = [raw(2, duration=30), raw(1)]
        svc.poll_all()
        assert queued_ids(dispatcher) == [vid(1), vid(2)]

    def test_live_and_unavailable_entries_wait(self, svc, feed, dispatcher):
        sub = svc.create(req())
        feed.entries = [raw(3, live_status="is_upcoming"), raw(2, title="[Private video]"), None, raw(1)]
        svc.poll_all()
        assert queued_ids(dispatcher) == [vid(1)]
        assert svc.get(sub.sub_id).seen_count == 1  # the upcoming one is looked at again
        feed.entries = [raw(3), raw(2, title="[Private video]"), None, raw(1)]
        svc.poll_all()
        assert queued_ids(dispatcher) == [vid(1), vid(3)]

    def test_at_most_a_batch_per_poll_the_rest_follows(self, svc, feed, dispatcher, monkeypatch):
        monkeypatch.setattr(sub_mod, "MAX_QUEUE_PER_POLL", 2)
        svc.create(req(PLAYLIST))
        feed.set(1, 2, 3, 4, 5)
        svc.poll_all()
        assert queued_ids(dispatcher) == [vid(1), vid(2)]
        svc.poll_all()
        svc.poll_all()
        assert queued_ids(dispatcher) == [vid(n) for n in range(1, 6)]

    def test_poll_failure_is_recorded_and_never_raised(self, svc, feed, dispatcher):
        sub = svc.create(req())
        feed.error = RuntimeError("network down")
        assert svc.poll_all() == {"checked": 1, "queued": 0}
        got = svc.get(sub.sub_id)
        assert got.last_error and got.last_check >= sub.last_check
        feed.error = None
        feed.set(1)
        svc.poll_all()
        assert svc.get(sub.sub_id).last_error is None  # recovered

    def test_one_broken_subscription_does_not_stop_the_others(self, svc, feed, dispatcher, monkeypatch):
        svc.create(req())
        svc.create(req(PLAYLIST))
        real = svc._poll_locked
        calls = []

        def flaky(sub):
            calls.append(sub.sub_id)
            if len(calls) == 1:
                raise RuntimeError("bug")
            return real(sub)

        monkeypatch.setattr(svc, "_poll_locked", flaky)
        assert svc.poll_all()["checked"] == 1 and len(calls) == 2

    def test_refused_submit_is_retried_later(self, svc, feed, dispatcher, server):
        server.max_active_jobs = 1
        sub = svc.create(req(), owner="alice")
        feed.set(2, 1)
        svc.poll_all()
        assert queued_ids(dispatcher) == [vid(1)]  # oldest first; alice is now at her limit
        assert svc.get(sub.sub_id, "alice").last_error.startswith("Waiting")
        assert svc.get(sub.sub_id, "alice").seen_count == 1  # the refused one stays unseen
        server.max_active_jobs = 5
        svc.poll_all()
        assert queued_ids(dispatcher) == [vid(1), vid(2)]
        assert svc.get(sub.sub_id, "alice").last_error is None

    def test_maintenance_skips_polling(self, svc, feed, dispatcher, server, monkeypatch):
        monkeypatch.setattr(server, "maintenance", True)
        svc.create(req(), owner="alice")
        feed.calls.clear()
        assert svc.poll_all() == {"checked": 0, "queued": 0} and feed.calls == []

    def test_overlapping_runs_are_refused(self, svc, feed):
        svc.create(req())
        feed.calls.clear()
        svc.r.set(f"{svc.prefix}:SUBS_LOCK", "1", ex=60)
        assert svc.poll_all() == {"checked": 0, "queued": 0} and feed.calls == []

    def test_deleted_while_polling_leaves_nothing_behind(self, svc, feed, dispatcher):
        sub = svc.create(req())
        feed.set(1)
        feed.on_call = lambda: svc.delete(sub.sub_id)
        svc.poll_all()
        assert svc.r.exists(svc.key(sub.sub_id), svc.seen_key(sub.sub_id)) == 0


class TestPauseCheckDelete:
    def test_paused_subscription_is_not_polled(self, svc, feed, dispatcher):
        sub = svc.create(req())
        assert svc.pause(sub.sub_id).enabled is False
        feed.set(1)
        feed.calls.clear()
        assert svc.poll_all() == {"checked": 0, "queued": 0}
        assert feed.calls == [] and dispatcher.items == []

        assert svc.resume(sub.sub_id).enabled is True
        assert svc.poll_all() == {"checked": 1, "queued": 1}

    def test_manual_check_works_while_paused(self, svc, feed, dispatcher):
        sub = svc.create(req())
        svc.pause(sub.sub_id)
        feed.set(1)
        got, new, error = svc.check(sub.sub_id)
        assert new == 1 and error is None and got.downloaded == 1 and got.enabled is False

    def test_manual_check_reports_errors(self, svc, feed):
        sub = svc.create(req())
        feed.error = RuntimeError("nope")
        got, new, error = svc.check(sub.sub_id)
        assert new == 0 and error and got.last_error == error

    def test_manual_check_conflicts_with_a_running_poll(self, svc, feed):
        sub = svc.create(req())
        svc.r.set(svc.lock_key(sub.sub_id), "1", ex=60)
        with pytest.raises(ConflictError):
            svc.check(sub.sub_id)

    def test_delete_removes_everything_but_keeps_downloads(self, svc, feed, dispatcher):
        feed.set(1)
        sub = svc.create(req())
        feed.set(2, 1)
        svc.poll_all()
        svc.delete(sub.sub_id)
        assert svc.list_for(None) == []
        assert svc.r.exists(svc.key(sub.sub_id), svc.seen_key(sub.sub_id)) == 0
        assert svc.r.zcard(svc.index) == 0
        assert len(jobs()) == 1  # the queued download is untouched
        with pytest.raises(NotFoundError):
            svc.get(sub.sub_id)
        with pytest.raises(NotFoundError):
            svc.delete(sub.sub_id)

    def test_list_is_newest_first_and_prunes_dead_index_entries(self, svc, feed):
        a = svc.create(req())
        b = svc.create(req(PLAYLIST))
        svc.r.zadd(svc.index, {"ghost": 1})
        assert [s.sub_id for s in svc.list_for(None)] == [b.sub_id, a.sub_id]
        assert svc.r.zscore(svc.index, "ghost") is None


# ───────────────────────── server mode ─────────────────────────


class TestOwnerIsolation:
    def test_visitors_only_see_and_control_their_own(self, svc, feed, server):
        a = svc.create(req(), owner="alice")
        b = svc.create(req(), owner="bob")  # same URL, different visitor: allowed
        assert [s.sub_id for s in svc.list_for("alice")] == [a.sub_id]
        assert [s.sub_id for s in svc.list_for("bob")] == [b.sub_id]
        for action in (svc.get, svc.pause, svc.resume, svc.check, svc.delete):
            with pytest.raises(NotFoundError):
                action(a.sub_id, "bob")
        assert svc.get(a.sub_id, "alice").enabled  # nothing changed
        assert len(svc.list_for(None)) == 2  # unscoped = everything (the periodic task)

    def test_downloads_belong_to_the_subscriber_and_land_in_deliveries(self, svc, feed, dispatcher, server):
        svc.create(req(), owner="alice")
        svc.create(req(PLAYLIST), owner="bob")
        feed.set(1)
        svc.poll_all()
        assert len(dispatcher.items) == 2
        assert len(jobs("alice")) == 1
        assert len(jobs("bob")) == 1
        job = jobs("alice")[0]
        assert job.folder.startswith(".deliveries")  # server-mode layout, no per-channel folder


# ───────────────────────── helpers / task / API ─────────────────────────


class TestHelpers:
    @pytest.mark.parametrize("entry,expected", [
        ({"duration": 59}, True), ({"duration": 60}, True), ({"duration": 61}, False),
        ({"duration": None}, False), ({"duration": 0}, False), ({}, False), (None, False),
        ({"url": "https://www.youtube.com/shorts/abc"}, True),
        ({"title": "so cute #SHORTS"}, True), ({"title": "A normal video", "duration": 600}, False),
    ])
    def test_is_short_entry(self, entry, expected):
        assert is_short_entry(entry) is expected

    def test_record_roundtrip_hides_derived_fields(self, svc, feed):
        sub = svc.create(req())
        stored = svc.r.hgetall(svc.key(sub.sub_id))
        assert "seen_count" not in stored
        assert Subscription.model_validate(svc.get(sub.sub_id).model_dump()).sub_id == sub.sub_id


class TestTask:
    def test_periodic_task_polls(self, svc, feed, dispatcher):
        svc.create(req())
        feed.set(1)
        poll_subscriptions_task()
        assert queued_ids(dispatcher) == [vid(1)]

    def test_periodic_task_survives_redis_trouble(self, svc, monkeypatch):
        monkeypatch.setattr(svc, "list_for", lambda owner=None: (_ for _ in ()).throw(RuntimeError("redis down")))
        poll_subscriptions_task()  # logged, not raised


class TestApi:
    @pytest.fixture
    def client(self, svc, downloads_dir):
        return TestClient(create_app())

    def test_full_lifecycle(self, client, feed, dispatcher):
        feed.set(1)
        r = client.post("/api/subscriptions", json={"url": CHANNEL, "format": "mp4", "quality": "720"})
        assert r.status_code == 201, r.text
        body = r.json()
        sid = body["sub_id"]
        assert body["kind"] == "channel" and body["title"] == "Some Channel" and body["seen_count"] == 1
        assert body["options"]["format"] == "mp4" and "owner" not in body

        assert [s["sub_id"] for s in client.get("/api/subscriptions").json()] == [sid]
        assert client.post("/api/subscriptions", json={"url": CHANNEL}).status_code == 409

        assert client.post(f"/api/subscriptions/{sid}/pause").json()["enabled"] is False
        feed.set(2, 1)
        checked = client.post(f"/api/subscriptions/{sid}/check").json()
        assert checked["new"] == 1 and checked["error"] is None and checked["subscription"]["downloaded"] == 1
        assert len(client.get("/api/downloads").json()) == 1
        assert client.post(f"/api/subscriptions/{sid}/resume").json()["enabled"] is True

        assert client.delete(f"/api/subscriptions/{sid}").status_code == 204
        assert client.get("/api/subscriptions").json() == []
        assert client.delete(f"/api/subscriptions/{sid}").status_code == 404
        assert client.post(f"/api/subscriptions/{sid}/check").status_code == 404

    def test_bad_url_is_422(self, client, feed):
        assert client.post("/api/subscriptions", json={"url": WATCH}).status_code == 422
        assert client.post("/api/subscriptions", json={"url": ""}).status_code == 422

    def test_config_exposes_the_limit(self, client):
        assert client.get("/api/config").json()["max_subscriptions"] == sub_mod.PERSONAL_MAX

    def test_server_mode_isolation_over_http(self, svc, feed, server):
        app = create_app()  # built after the switch: the visitor middleware exists only in server mode
        alice, bob = TestClient(app), TestClient(app)
        for c in (alice, bob):
            c.get("/api/config")
            assert c.cookies.get(VISITOR_COOKIE)
        sid = alice.post("/api/subscriptions", json={"url": CHANNEL}).json()["sub_id"]
        assert [s["sub_id"] for s in alice.get("/api/subscriptions").json()] == [sid]
        assert bob.get("/api/subscriptions").json() == []
        assert bob.post(f"/api/subscriptions/{sid}/pause").status_code == 404
        assert bob.post(f"/api/subscriptions/{sid}/check").status_code == 404
        assert bob.delete(f"/api/subscriptions/{sid}").status_code == 404
        assert alice.get("/api/config").json()["max_subscriptions"] == 20
        assert alice.delete(f"/api/subscriptions/{sid}").status_code == 204
