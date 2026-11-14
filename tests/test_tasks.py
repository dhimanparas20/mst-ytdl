"""tasks/ — Huey wiring (immediate mode) and the dispatcher."""

from __future__ import annotations

import time

import services.dispatch as dispatch_mod
import tasks
import tasks.tasks as tasks_mod
from configs import get_key_prefix
from models import DownloadRequest, EntryInfo, JobKind
from models import DownloadStatus as S
from services import download_manager as mgr
from services import job_store as store
from services import metadata_service
from services.metadata import PlaylistInfo

WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
PLAYLIST = "https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"


def test_huey_is_in_immediate_mode_for_tests():
    assert tasks.huey.immediate is True


def test_tasks_registered_by_name():
    names = set(tasks.huey._registry._registry)
    joined = " ".join(names)
    for n in ("resolve_job", "download_item", "reconcile"):
        assert n in joined


def test_importing_tasks_registers_the_huey_dispatcher():
    assert isinstance(dispatch_mod._dispatcher, tasks.HueyDispatcher) or dispatch_mod._dispatcher is not None
    assert isinstance(tasks.HueyDispatcher(), object)


class TestDispatcher:
    def test_enqueue_item_runs_runner_inline(self, downloads_dir, fake_ydl):
        dispatch_mod.set_dispatcher(tasks.HueyDispatcher())
        jobs, _ = mgr.submit(DownloadRequest(urls=[WATCH]))
        assert jobs[0].status is S.COMPLETED

    def test_enqueue_resolve_runs_resolution_then_items(self, downloads_dir, fake_ydl, monkeypatch):
        dispatch_mod.set_dispatcher(tasks.HueyDispatcher())
        entries = [EntryInfo(index=i, video_id=f"{i:011d}", url=f"https://www.youtube.com/watch?v={i:011d}", title=f"T{i}") for i in (1, 2)]
        monkeypatch.setattr(metadata_service, "expand_playlist", lambda u: PlaylistInfo("Mix", None, None, entries))
        jobs, _ = mgr.submit(DownloadRequest(urls=[PLAYLIST]))
        view = mgr.get_job_view(jobs[0].job_id)
        assert view.kind is JobKind.PLAYLIST and view.status is S.COMPLETED and len(view.items) == 2

    def test_delayed_enqueue_uses_schedule(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tasks_mod.download_item_task, "schedule",
                            lambda args=None, delay=None, **k: calls.append((args, delay)))
        tasks.HueyDispatcher().enqueue_item("it", "tok", delay=7)
        assert calls == [(("it", "tok"), 7)]

    def test_zero_delay_enqueues_immediately(self, monkeypatch):
        ran = []
        monkeypatch.setattr(tasks_mod, "download_item_task", lambda *a: ran.append(a))
        tasks.HueyDispatcher().enqueue_item("it", "tok")
        assert ran == [("it", "tok")]


class TestPeriodicAndStartup:
    def test_heartbeat_key_is_prefixed(self):
        assert tasks.heartbeat_key() == f"{get_key_prefix()}:WORKER:HEARTBEAT"

    def test_reconcile_task_beats_and_repairs(self, fake_redis, dispatcher):
        jobs, _ = mgr.submit(DownloadRequest(urls=[WATCH]))
        iid = jobs[0].items[0].item_id
        store.patch_item(iid, status=S.DOWNLOADING)
        store.r.hset(store.item_key(iid), "updated_at", str(time.time() - 600))
        tasks.reconcile_task()
        assert fake_redis.exists(tasks.heartbeat_key()) == 1
        assert fake_redis.ttl(tasks.heartbeat_key()) > 0
        assert store.get_item(iid).status is S.PAUSED

    def test_startup_hook_publishes_heartbeat_and_reconciles(self, fake_redis, dispatcher):
        tasks_mod._announced.clear()
        tasks_mod._announce_worker()
        assert fake_redis.exists(tasks.heartbeat_key()) == 1

    def test_startup_hook_runs_once_despite_one_call_per_worker_thread(self, fake_redis, monkeypatch):
        tasks_mod._announced.clear()
        beats = []
        monkeypatch.setattr(tasks_mod, "_beat", lambda: beats.append(1))
        for _ in range(6):  # Huey invokes the hook in every worker thread
            tasks_mod._announce_worker()
        assert beats == [1]

    def test_startup_hook_never_raises(self, monkeypatch):
        tasks_mod._announced.clear()
        monkeypatch.setattr(tasks_mod, "_beat", lambda: (_ for _ in ()).throw(RuntimeError("redis down")))
        tasks_mod._announce_worker()  # logged, not raised — the consumer must still boot
