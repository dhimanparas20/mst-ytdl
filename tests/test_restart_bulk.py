"""Restart/replace and bulk job actions — manager level plus the runner honouring `force`."""

from __future__ import annotations

import pytest

import services.downloader as dl_mod
from models import DownloadRequest, EntryInfo
from models import DownloadStatus as S
from services import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    metadata_service,
)
from services import (
    download_manager as mgr,
)
from services import (
    download_runner as runner,
)
from services import (
    job_store as store,
)
from services.metadata import PlaylistInfo
from tests.fakes import FakeYDL, make_download_error

WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
PLAYLIST = "https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"


def single(**kw):
    v = mgr.submit(DownloadRequest(urls=[WATCH], **kw))[0][0]
    return v.job_id, v.items[0].item_id


def force(item_id, **fields):
    store.patch_item(item_id, **fields)


@pytest.fixture
def playlist(monkeypatch):
    entries = [EntryInfo(index=i, video_id=f"{i:011d}", url=f"https://www.youtube.com/watch?v={i:011d}", title=f"S{i}") for i in range(1, 4)]
    monkeypatch.setattr(metadata_service, "expand_playlist", lambda u: PlaylistInfo("Mix", None, None, entries))


def playlist_job():
    jid = mgr.submit(DownloadRequest(urls=[PLAYLIST]))[0][0].job_id
    mgr.resolve_job(jid)
    return jid


# ───────────────────────── restart (manager) ─────────────────────────


class TestRestartItem:
    @pytest.mark.parametrize("status", [S.FAILED, S.CANCELLED, S.PAUSED, S.COMPLETED])
    def test_restartable_states(self, dispatcher, status):
        _, iid = single()
        force(iid, status=status, error="boom", attempts=3, percent=77.0, downloaded_bytes=5, speed=1.0)
        old_token = dispatcher.last_token(iid)
        it = mgr.restart_item(iid)
        assert it.status is S.QUEUED and it.force is True and it.attempts == 0 and it.error is None
        assert it.percent == 0 and it.downloaded_bytes == 0 and it.speed is None and it.finished_at is None
        assert it.token and it.token != old_token and dispatcher.last_token(iid) == it.token

    @pytest.mark.parametrize("status", [S.QUEUED, S.DOWNLOADING, S.PROCESSING])
    def test_busy_states_conflict(self, dispatcher, status):
        _, iid = single()
        force(iid, status=status)
        n = len(dispatcher.items)
        with pytest.raises(ConflictError):
            mgr.restart_item(iid)
        assert len(dispatcher.items) == n

    def test_unknown(self):
        with pytest.raises(NotFoundError):
            mgr.restart_item("ghost")

    def test_partials_are_deleted(self, dispatcher, tmp_path):
        _, iid = single()
        part = tmp_path / "x.mp3.part"
        part.write_bytes(b"half")
        force(iid, status=S.PAUSED, partials=[str(tmp_path / "x.mp3")])
        mgr.restart_item(iid)
        assert not part.exists() and store.get_item(iid).partials == []

    def test_completed_keeps_its_old_file_until_replaced(self, dispatcher, downloads_dir):
        _, iid = single()
        old = downloads_dir / "d" / "a.mp3"
        old.parent.mkdir()
        old.write_bytes(b"original")
        force(iid, status=S.COMPLETED, filepath="d/a.mp3")
        mgr.restart_item(iid)
        assert old.read_bytes() == b"original"  # a failed replacement must not destroy it

    def test_restart_job_and_resolve_failed_playlist(self, dispatcher, playlist):
        jid = playlist_job()
        ids = store.get_item_ids(jid)
        force(ids[0], status=S.COMPLETED)
        force(ids[1], status=S.FAILED)
        force(ids[2], status=S.DOWNLOADING)  # busy → skipped, no error
        v = mgr.restart_job(jid)
        assert [i.status for i in v.items] == [S.QUEUED, S.QUEUED, S.DOWNLOADING]
        assert v.items[0].force and v.items[1].force and not v.items[2].force

    def test_restart_job_reruns_failed_expansion(self, dispatcher):
        jid = mgr.submit(DownloadRequest(urls=[PLAYLIST]))[0][0].job_id
        store.update_job(jid, resolve_state="failed", resolve_error="net")
        dispatcher.resolves.clear()
        mgr.restart_job(jid)
        assert dispatcher.resolves == [jid]


# ───────────────────────── runner honours force ─────────────────────────


class TestRunnerForce:
    @pytest.fixture(autouse=True)
    def _env(self, downloads_dir, fake_ydl, dispatcher, monkeypatch):
        monkeypatch.setattr(dl_mod, "_CHECK_INTERVAL", 0.0)

    def run_restarted(self, dispatcher, iid):
        mgr.restart_item(iid)
        return runner.run(iid, dispatcher.last_token(iid))

    def test_replace_overwrites_an_existing_file(self, dispatcher, downloads_dir):
        jid, iid = single()
        assert runner.run(iid, dispatcher.last_token(iid)) == "completed"
        final = downloads_dir / store.get_item(iid).filepath
        final.write_bytes(b"tampered")
        calls = FakeYDL.calls
        assert self.run_restarted(dispatcher, iid) == "completed"
        assert FakeYDL.calls == calls + 1  # really re-downloaded (not skipped as "exists")
        assert FakeYDL.last_opts["overwrites"] is True
        assert final.stat().st_size == 1000 and final.read_bytes() != b"tampered"

    def test_without_restart_an_existing_file_is_skipped(self, dispatcher):
        _, iid = single()
        runner.run(iid, dispatcher.last_token(iid))
        calls = FakeYDL.calls
        job = store.get_item(iid).job_id
        # second identical job: skip-existing applies
        v = mgr.submit(DownloadRequest(urls=[WATCH]))[0][0]
        runner.run(v.items[0].item_id, dispatcher.last_token(v.items[0].item_id))
        assert FakeYDL.calls == calls and job != v.job_id

    def test_force_flag_is_cleared_after_the_run(self, dispatcher):
        _, iid = single()
        runner.run(iid, dispatcher.last_token(iid))
        self.run_restarted(dispatcher, iid)
        assert store.get_item(iid).force is False

    def test_force_cleared_after_failure_too(self, dispatcher):
        _, iid = single()
        runner.run(iid, dispatcher.last_token(iid))
        FakeYDL.fail_with = make_download_error("Private video")
        assert self.run_restarted(dispatcher, iid) == "failed"
        assert store.get_item(iid).force is False

    def test_force_survives_an_auto_retry(self, dispatcher):
        _, iid = single()
        runner.run(iid, dispatcher.last_token(iid))
        # fail only the next call (FakeYDL.calls already counts the first, successful run)
        FakeYDL.fail_with, FakeYDL.fail_times = make_download_error("Connection reset"), FakeYDL.calls + 1
        assert self.run_restarted(dispatcher, iid) == "retry"
        assert store.get_item(iid).force is True  # the retry must still replace, not skip

    def test_restart_of_failed_item_downloads_fresh(self, dispatcher, downloads_dir):
        _, iid = single()
        FakeYDL.fail_with = make_download_error("Private video")
        runner.run(iid, dispatcher.last_token(iid))
        FakeYDL.fail_with = None
        assert self.run_restarted(dispatcher, iid) == "completed"
        assert store.get_item(iid).status is S.COMPLETED


# ───────────────────────── bulk ─────────────────────────


class TestBulkJobs:
    def three(self):
        return [single()[0] for _ in range(3)]

    def test_pause_then_resume(self, dispatcher):
        jobs = self.three()
        r = mgr.bulk_jobs(jobs, "pause")
        assert r == {"affected": 3, "skipped": 0, "errors": []}
        assert all(store.get_items(j)[0].status is S.PAUSED for j in jobs)
        assert mgr.bulk_jobs(jobs, "resume")["affected"] == 3
        assert all(store.get_items(j)[0].status is S.QUEUED for j in jobs)

    def test_inapplicable_jobs_are_skipped_not_errors(self, dispatcher):
        a, b, c = self.three()
        force(store.get_item_ids(b)[0], status=S.COMPLETED)
        r = mgr.bulk_jobs([a, b, c], "pause")
        assert (r["affected"], r["skipped"], r["errors"]) == (2, 1, [])

    def test_missing_ids_are_reported_and_do_not_abort(self, dispatcher):
        a = self.three()[0]
        r = mgr.bulk_jobs(["ghost", a, "ghost2"], "pause")
        assert r["affected"] == 1 and [e["id"] for e in r["errors"]] == ["ghost", "ghost2"]

    def test_duplicates_are_applied_once(self, dispatcher):
        a = self.three()[0]
        assert mgr.bulk_jobs([a, a, a], "pause")["affected"] == 1

    def test_cancel_retry_restart(self, dispatcher):
        jobs = self.three()
        assert mgr.bulk_jobs(jobs, "cancel")["affected"] == 3
        assert all(store.get_items(j)[0].status is S.CANCELLED for j in jobs)
        assert mgr.bulk_jobs(jobs, "retry")["affected"] == 3
        assert all(store.get_items(j)[0].status is S.QUEUED for j in jobs)
        for j in jobs:
            force(store.get_item_ids(j)[0], status=S.COMPLETED)
        assert mgr.bulk_jobs(jobs, "restart")["affected"] == 3
        assert all(store.get_items(j)[0].force for j in jobs)

    def test_cancel_includes_a_resolving_playlist(self, dispatcher):
        jid = mgr.submit(DownloadRequest(urls=[PLAYLIST]))[0][0].job_id
        assert mgr.bulk_jobs([jid], "cancel")["affected"] == 1
        assert store.get_job(jid).resolve_error == "Cancelled"

    def test_remove_and_remove_files(self, dispatcher, downloads_dir):
        a, b = single()[0], single()[0]
        f = downloads_dir / "x" / "a.mp3"
        f.parent.mkdir()
        f.write_bytes(b"x")
        force(store.get_item_ids(a)[0], status=S.COMPLETED, filepath="x/a.mp3")
        assert mgr.bulk_jobs([a], "remove")["affected"] == 1
        assert f.exists() and store.get_job(a) is None
        force(store.get_item_ids(b)[0], status=S.COMPLETED, filepath="x/a.mp3")
        assert mgr.bulk_jobs([b], "remove-files")["affected"] == 1
        assert not f.exists()

    def test_unknown_action(self, dispatcher):
        with pytest.raises(InvalidRequestError):
            mgr.bulk_jobs(self.three(), "explode")
