"""services/downloader.py — the runner, driven by a scripted yt-dlp (no network)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import services.downloader as dl_mod
from configs import get_settings
from models import (
    DownloadItem,
    DownloadJob,
    DownloadOptions,
    JobKind,
)
from models import (
    DownloadStatus as S,
)
from models import (
    MediaFormat as F,
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
from services import (
    runtime_service,
    slot_limiter,
)
from tests.fakes import FakeYDL, make_download_error, wait_for

PL_STEPS = 10


def make(fmt=F.MP3, *, kind=JobKind.SINGLE, folder="2026-01-01", index=1, title="", playlist_size=0, token="tok1", quality="best", overwrite=False, numbering=True, **jobkw):
    """Persist a job + one queued item and return (item_id, token)."""
    job = DownloadJob(job_id="job1", url="https://www.youtube.com/watch?v=abc123DEF45", kind=kind, folder=folder,
                      options=DownloadOptions(format=fmt, quality=quality, overwrite=overwrite, numbering=numbering), total_items=1, playlist_size=playlist_size, **jobkw)
    store.create_job(job)
    item = DownloadItem(item_id="it1", job_id="job1", index=index, url=job.url, title=title, token=token)
    store.add_items([item])
    return "it1", token


def make_more(n):
    """n extra independent single jobs; returns [(item_id, token)]."""
    out = []
    for k in range(n):
        j = DownloadJob(job_id=f"jx{k}", url="https://www.youtube.com/watch?v=abc123DEF45", folder=f"d{k}",
                        options=DownloadOptions(format=F.MP3), total_items=1)
        store.create_job(j)
        store.add_items([DownloadItem(item_id=f"ix{k}", job_id=j.job_id, url=j.url, token=f"t{k}")])
        out.append((f"ix{k}", f"t{k}"))
    return out


@pytest.fixture(autouse=True)
def _env(downloads_dir, fake_ydl, dispatcher, monkeypatch):
    """Every test: temp downloads dir, fake yt-dlp, recording dispatcher, per-tick progress."""
    monkeypatch.setattr(dl_mod, "_CHECK_INTERVAL", 0.0)
    return None


def tmp_dir(downloads_dir, item_id="it1") -> Path:
    """Per-item scratch directory (partial files, intermediates, sidecars live here)."""
    return downloads_dir / ".tmp" / item_id


def final_file(downloads_dir, folder="2026-01-01", name="Fake Song.mp3") -> Path:
    return downloads_dir / folder / name


# ───────────────────────── success path ─────────────────────────


class TestSuccess:
    def test_completes_and_records_everything(self, downloads_dir):
        iid, tok = make()
        assert runner.run(iid, tok) == "completed"
        it = store.get_item(iid)
        f = final_file(downloads_dir)
        assert f.exists() and f.stat().st_size == PL_STEPS * 100
        assert it.status is S.COMPLETED and it.percent == 100.0 and it.error is None
        assert it.filepath == "2026-01-01/Fake Song.mp3" and it.filesize == f.stat().st_size
        assert it.attempts == 1 and it.token == "" and it.control is None and it.stage == ""
        assert it.speed is None and it.eta is None and it.finished_at and it.started_at
        assert not tmp_dir(downloads_dir).exists()  # scratch dir is removed after success

    def test_metadata_applied_to_item_and_single_job(self):
        iid, tok = make()
        runner.run(iid, tok)
        it = store.get_item(iid)
        assert (it.title, it.uploader, it.duration, it.video_id) == ("Fake Song", "Fake Channel", 42, "abc123DEF45")
        assert it.thumbnail == "https://example.test/t.jpg"
        j = store.get_job("job1")
        assert j.title == "Fake Song" and j.uploader == "Fake Channel"

    def test_playlist_item_keeps_its_playlist_title_and_job_title(self):
        iid, tok = make(kind=JobKind.PLAYLIST, title="Track From Playlist", title_x=None) if False else make(kind=JobKind.PLAYLIST, title="Track From Playlist")
        store.update_job("job1", title="The Playlist")
        runner.run(iid, tok)
        assert store.get_item(iid).title == "Track From Playlist"
        assert store.get_job("job1").title == "The Playlist"  # item metadata never renames the playlist

    def test_creates_missing_folder(self, downloads_dir):
        iid, tok = make(folder="deep/nested/dir")
        runner.run(iid, tok)
        assert (downloads_dir / "deep/nested/dir/Fake Song.mp3").exists()

    @pytest.mark.parametrize("fmt,ext", [(F.MP3, "mp3"), (F.M4A, "m4a"), (F.FLAC, "flac"), (F.MP4, "mp4"), (F.MKV, "mkv")])
    def test_extension_follows_format(self, downloads_dir, fmt, ext):
        iid, tok = make(fmt)
        runner.run(iid, tok)
        assert final_file(downloads_dir, name=f"Fake Song.{ext}").exists()

    def test_fallback_path_from_result_when_post_hook_never_fires(self, downloads_dir):
        FakeYDL.call_post_hook = False
        iid, tok = make()
        assert runner.run(iid, tok) == "completed"
        assert store.get_item(iid).filepath.endswith("Fake Song.mp3")

    def test_slot_released_after_run(self):
        iid, tok = make()
        runner.run(iid, tok)
        assert slot_limiter.holders() == []

    def test_existing_output_is_skipped_not_reprocessed(self, downloads_dir):
        f = final_file(downloads_dir)
        f.parent.mkdir(parents=True)
        f.write_bytes(b"already here")
        iid, tok = make()
        assert runner.run(iid, tok) == "completed"
        it = store.get_item(iid)
        assert FakeYDL.calls == 0  # no download, no post-processing
        assert f.read_bytes() == b"already here"  # untouched
        assert it.status is S.COMPLETED and it.filepath == "2026-01-01/Fake Song.mp3" and it.filesize == 12
        assert it.title == "Fake Song"  # metadata still filled in for the UI

    def test_empty_existing_file_is_not_trusted(self, downloads_dir):
        f = final_file(downloads_dir)
        f.parent.mkdir(parents=True)
        f.write_bytes(b"")  # zero-byte leftover of a crash
        iid, tok = make()
        runner.run(iid, tok)
        assert FakeYDL.calls == 1 and f.stat().st_size == PL_STEPS * 100

    def test_same_title_other_format_is_not_a_duplicate(self, downloads_dir):
        f = final_file(downloads_dir, name="Fake Song.mp3")
        f.parent.mkdir(parents=True)
        f.write_bytes(b"mp3 version")
        iid, tok = make(F.MP4, quality="720")
        runner.run(iid, tok)
        assert FakeYDL.calls == 1 and final_file(downloads_dir, name="Fake Song.mp4").exists()
        assert f.read_bytes() == b"mp3 version"

    def test_overwrite_option_forces_a_fresh_download(self, downloads_dir):
        f = final_file(downloads_dir)
        f.parent.mkdir(parents=True)
        f.write_bytes(b"old")
        iid, tok = make(overwrite=True)
        runner.run(iid, tok)
        assert FakeYDL.calls == 1 and FakeYDL.last_opts["overwrites"] is True
        assert f.stat().st_size == PL_STEPS * 100

    def test_numbered_playlist_item_matches_numbered_existing_file(self, downloads_dir):
        f = downloads_dir / "Mix" / "03 - Fake Song.mp3"
        f.parent.mkdir()
        f.write_bytes(b"there")
        iid, tok = make(kind=JobKind.PLAYLIST, index=3, playlist_size=9, folder="Mix")
        runner.run(iid, tok)
        assert FakeYDL.calls == 0 and f.read_bytes() == b"there"

    def test_extraction_happens_once_not_twice(self):
        iid, tok = make()
        runner.run(iid, tok)
        assert FakeYDL.instances[0].extract_calls == 1  # info reused for the download

    def test_runtime_speed_limit_reaches_yt_dlp(self):
        runtime_service.update(speed_limit=777)
        iid, tok = make()
        runner.run(iid, tok)
        assert FakeYDL.last_opts["ratelimit"] == 777


class TestNumbering:
    @pytest.mark.parametrize("index,size,expected", [(7, 12, "07 - "), (3, 5, "03 - "), (7, 250, "007 - "), (120, 250, "120 - ")])
    def test_prefix_width_follows_playlist_size(self, downloads_dir, index, size, expected):
        iid, tok = make(kind=JobKind.PLAYLIST, index=index, playlist_size=size, folder="Mix")
        runner.run(iid, tok)
        assert (downloads_dir / "Mix" / f"{expected}Fake Song.mp3").exists()

    def test_singles_are_not_numbered(self, downloads_dir):
        iid, tok = make(index=5)
        runner.run(iid, tok)
        assert final_file(downloads_dir).exists()

    def test_numbering_can_be_disabled_per_download(self, downloads_dir):
        iid, tok = make(kind=JobKind.PLAYLIST, index=2, playlist_size=9, folder="Mix", numbering=False)
        runner.run(iid, tok)
        assert (downloads_dir / "Mix" / "Fake Song.mp3").exists()
        assert not (downloads_dir / "Mix" / "02 - Fake Song.mp3").exists()

    def test_request_default_follows_the_env_setting(self, monkeypatch):
        from models import DownloadRequest
        from services import download_manager

        req = DownloadRequest(urls=["https://www.youtube.com/watch?v=abc123DEF45"])
        assert download_manager.resolve_options(req).numbering is True
        monkeypatch.setattr(get_settings().download, "playlist_numbering", False)
        assert download_manager.resolve_options(req).numbering is False
        # an explicit choice from the UI wins over the env default
        on = DownloadRequest(urls=req.urls, numbering=True)
        assert download_manager.resolve_options(on).numbering is True


# ───────────────────────── progress ─────────────────────────


@pytest.fixture
def percents(monkeypatch):
    seen = []
    real = store.patch_item

    def spy(item_id, **fields):
        if "percent" in fields:
            seen.append((fields["percent"], fields.get("stage")))
        return real(item_id, **fields)

    monkeypatch.setattr(store, "patch_item", spy)
    return seen


class TestProgress:
    def test_single_stream_is_monotonic_and_capped_below_100(self, percents):
        FakeYDL.steps = 20
        iid, tok = make()
        runner.run(iid, tok)
        values = [p for p, _ in percents]
        assert len(values) >= 15 and values == sorted(values)
        assert max(values) <= 99.5 and values[-1] > 90
        assert {s for _, s in percents} == {"downloading"}

    def test_video_plus_audio_weights_video_90_percent(self, percents):
        FakeYDL.steps, FakeYDL.streams = 10, 2
        iid, tok = make(F.MP4, quality="720")
        runner.run(iid, tok)
        video = [p for p, s in percents if s == "video"]
        audio = [p for p, s in percents if s == "audio"]
        assert video and audio
        assert max(video) <= 90.0 and min(audio) >= 90.0
        assert [p for p, _ in percents] == sorted(p for p, _ in percents)

    def test_progress_fields_are_stored(self):
        seen = []
        FakeYDL.before_finish = lambda: seen.append(store.get_item("it1"))
        FakeYDL.steps = 5
        iid, tok = make()
        runner.run(iid, tok)
        it = seen[0]
        assert it.status is S.DOWNLOADING and it.speed == 1000.0 and it.downloaded_bytes == 500 and it.total_bytes == 500

    def test_processing_state_visible_during_postprocessing(self):
        snap = {}
        FakeYDL.probe = lambda: snap.update(it=store.get_item("it1"))
        iid, tok = make()
        runner.run(iid, tok)
        it = snap["it"]
        assert it.status is S.PROCESSING and it.stage == "converting audio" and it.percent == 99.5 and it.speed is None

    def test_total_bytes_unknown_for_two_stream_jobs_until_done(self):
        FakeYDL.streams = 2
        snap = []
        FakeYDL.before_finish = lambda: snap.append(store.get_item("it1").total_bytes)
        iid, tok = make(F.MP4, quality="720")
        runner.run(iid, tok)
        assert snap == [None]
        assert store.get_item(iid).total_bytes == store.get_item(iid).filesize


# ───────────────────────── failures + retry ─────────────────────────


class TestFailures:
    def test_permanent_error_fails_immediately(self, dispatcher):
        FakeYDL.fail_with = make_download_error("[youtube] x: Private video. Sign in if you've been granted access")
        iid, tok = make()
        assert runner.run(iid, tok) == "failed"
        it = store.get_item(iid)
        assert it.status is S.FAILED and "Private video" in it.error and it.token == "" and it.finished_at
        assert it.attempts == 1 and dispatcher.items == []
        assert it.partials  # leftovers remembered so a later cancel can delete them

    def test_transient_error_requeues_with_delay_and_new_token(self, dispatcher, task_settings):
        FakeYDL.fail_with = make_download_error("HTTP Error 503: Service Unavailable")
        iid, tok = make()
        assert runner.run(iid, tok) == "retry"
        it = store.get_item(iid)
        assert it.status is S.QUEUED and it.token and it.token != tok
        assert it.error.startswith("Retrying (1/") and "503" in it.error
        assert dispatcher.items == [(iid, it.token, task_settings.retry_delay)]

    def test_retry_resumes_and_then_succeeds(self, dispatcher, downloads_dir):
        FakeYDL.fail_with, FakeYDL.fail_times = make_download_error("Connection reset by peer"), 1
        iid, tok = make()
        assert runner.run(iid, tok) == "retry"
        part = tmp_dir(downloads_dir) / "Fake Song.mp3.part"
        assert part.exists()  # half-downloaded data kept
        _, new_tok, _ = dispatcher.items[-1]
        assert runner.run(iid, new_tok) == "completed"
        it = store.get_item(iid)
        assert it.status is S.COMPLETED and it.attempts == 2 and it.error is None
        assert final_file(downloads_dir).stat().st_size == PL_STEPS * 100  # not duplicated, not truncated

    def test_gives_up_after_retry_budget(self, dispatcher, task_settings, monkeypatch):
        monkeypatch.setattr(task_settings, "retry_count", 2)
        FakeYDL.fail_with = make_download_error("timed out")
        iid, tok = make()
        outcomes = []
        for _ in range(5):
            outcomes.append(runner.run(iid, tok))
            if outcomes[-1] != "retry":
                break
            tok = dispatcher.last_token(iid)
        assert outcomes == ["retry", "retry", "failed"]
        assert store.get_item(iid).attempts == 3 and store.get_item(iid).status is S.FAILED

    def test_zero_retries_means_one_attempt(self, dispatcher, task_settings, monkeypatch):
        monkeypatch.setattr(task_settings, "retry_count", 0)
        FakeYDL.fail_with = make_download_error("timed out")
        iid, tok = make()
        assert runner.run(iid, tok) == "failed" and dispatcher.items == []

    def test_unexpected_exception_is_failed_not_retried(self, dispatcher):
        FakeYDL.fail_with = KeyError("boom")
        iid, tok = make()
        assert runner.run(iid, tok) == "failed"
        assert store.get_item(iid).status is S.FAILED and dispatcher.items == []

    def test_ffmpeg_missing(self, monkeypatch):
        monkeypatch.setattr(dl_mod, "get_ffmpeg_path", lambda: None)
        iid, tok = make()
        assert runner.run(iid, tok) == "failed"
        assert "ffmpeg is not installed" in store.get_item(iid).error and FakeYDL.calls == 0

    def test_output_file_missing(self):
        FakeYDL.write_final = False
        iid, tok = make()
        assert runner.run(iid, tok) == "failed"
        assert "could not be located" in store.get_item(iid).error

    def test_live_stream_refused(self):
        FakeYDL.live = True
        iid, tok = make()
        assert runner.run(iid, tok) == "failed"
        assert "Live streams" in store.get_item(iid).error and FakeYDL.calls == 0

    @pytest.mark.parametrize("folder", ["../escape", "../../etc", "a/../../b"])
    def test_refuses_to_write_outside_downloads_dir(self, folder, tmp_path):
        iid, tok = make(folder=folder)
        assert runner.run(iid, tok) == "failed"
        assert "outside the downloads directory" in store.get_item(iid).error
        assert not (tmp_path / "escape").exists()

    def test_failure_releases_slot(self):
        FakeYDL.fail_with = make_download_error("Private video")
        iid, tok = make()
        runner.run(iid, tok)
        assert slot_limiter.holders() == []


# ───────────────────────── staleness ─────────────────────────


class TestStale:
    def test_wrong_token(self):
        iid, tok = make()
        assert runner.run(iid, "not-the-token") == "stale"
        assert store.get_item(iid).status is S.QUEUED and FakeYDL.calls == 0

    def test_missing_item_and_missing_job(self):
        assert runner.run("ghost", "t") == "stale"
        iid, tok = make()
        store.r.delete(store.job_key("job1"))
        assert runner.run(iid, tok) == "stale"

    @pytest.mark.parametrize("status", [S.PAUSED, S.COMPLETED, S.FAILED, S.CANCELLED, S.DOWNLOADING])
    def test_only_queued_items_run(self, status):
        iid, tok = make()
        store.patch_item(iid, status=status)
        assert runner.run(iid, tok) == "stale" and FakeYDL.calls == 0

    def test_duplicate_delivery_runs_once(self, downloads_dir):
        FakeYDL.step_delay = 0.01
        FakeYDL.steps = 20
        iid, tok = make()
        results = []
        gate = threading.Barrier(2)

        def go():
            gate.wait()
            results.append(runner.run(iid, tok))

        ts = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(results) == ["completed", "stale"] and FakeYDL.calls == 1
        assert final_file(downloads_dir).stat().st_size == 20 * 100

    def test_stale_task_cannot_free_a_newer_runs_slot(self):
        """A superseded task must not release the slot of the live run (per-run holders)."""
        iid, tok = make()
        slot_limiter.acquire(f"{iid}:live-run", 1)
        # a stale run for the same item: token mismatch → exits without touching the live holder
        assert runner.run(iid, "old-token") == "stale"
        assert slot_limiter.holders() == [f"{iid}:live-run"]


# ───────────────────────── pause / resume / cancel ─────────────────────────


def run_in_thread(iid, tok):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", runner.run(iid, tok)))
    t.start()
    return t, out


class TestPauseResume:
    def test_pause_keeps_partial_then_resume_continues(self, downloads_dir, dispatcher):
        FakeYDL.steps, FakeYDL.step_delay = 200, 0.005
        iid, tok = make()
        t, out = run_in_thread(iid, tok)
        wait_for(lambda: (store.get_item(iid).percent or 0) > 10)
        mgr.pause_item(iid)
        t.join(10)
        assert out["r"] == "paused"

        it = store.get_item(iid)
        part = tmp_dir(downloads_dir) / "Fake Song.mp3.part"
        assert it.status is S.PAUSED and it.control is None and it.token == "" and it.speed is None
        assert part.exists() and 0 < part.stat().st_size < 200 * 100
        assert it.partials and slot_limiter.holders() == []
        written = part.stat().st_size

        mgr.resume_item(iid)
        new_tok = dispatcher.last_token(iid)
        assert new_tok and new_tok != tok
        assert runner.run(iid, new_tok) == "completed"
        assert final_file(downloads_dir).stat().st_size == 200 * 100  # resumed from `written`, not restarted
        assert written > 0 and store.get_item(iid).attempts == 2

    def test_pause_survives_multiple_cycles(self, downloads_dir, dispatcher):
        FakeYDL.steps, FakeYDL.step_delay = 100, 0.005
        iid, tok = make()
        for _cycle in range(3):
            t, out = run_in_thread(iid, tok)
            wait_for(lambda: store.get_item(iid).status is S.DOWNLOADING)
            mgr.pause_item(iid)
            t.join(10)
            assert out["r"] == "paused"
            mgr.resume_item(iid)
            tok = dispatcher.last_token(iid)
        assert runner.run(iid, tok) == "completed"
        assert final_file(downloads_dir).stat().st_size == 100 * 100

    def test_pausing_a_waiting_item_exits_the_waiting_task(self, dispatcher):
        runtime_service.update(max_parallel=1)
        slot_limiter.acquire("blocker:run", 1)  # occupy the only slot
        iid, tok = make()
        t, out = run_in_thread(iid, tok)  # waits for a slot
        wait_for(lambda: not t.is_alive() or True)
        mgr.pause_item(iid)
        t.join(5)
        assert out["r"] == "stale" and FakeYDL.calls == 0
        assert store.get_item(iid).status is S.PAUSED

    def test_pause_before_start_makes_task_stale(self):
        iid, tok = make()
        mgr.pause_item(iid)
        assert runner.run(iid, tok) == "stale" and FakeYDL.calls == 0

    def test_pause_requested_during_processing_still_completes(self):
        FakeYDL.probe = lambda: mgr.pause_item("it1")  # lands while ffmpeg "runs"
        iid, tok = make()
        assert runner.run(iid, tok) == "completed"
        assert store.get_item(iid).status is S.COMPLETED


class TestCancel:
    def test_cancel_mid_download_removes_partials(self, downloads_dir):
        FakeYDL.steps, FakeYDL.step_delay = 200, 0.005
        iid, tok = make()
        t, out = run_in_thread(iid, tok)
        wait_for(lambda: (store.get_item(iid).percent or 0) > 5)
        mgr.cancel_item(iid)
        t.join(10)
        assert out["r"] == "cancelled"
        it = store.get_item(iid)
        assert it.status is S.CANCELLED and it.percent == 0 and it.downloaded_bytes == 0 and it.token == ""
        assert not tmp_dir(downloads_dir).exists()
        assert list((downloads_dir / "2026-01-01").glob("*")) == []
        assert slot_limiter.holders() == []

    def test_cancel_then_retry_downloads_from_scratch(self, downloads_dir, dispatcher):
        FakeYDL.steps, FakeYDL.step_delay = 100, 0.003
        iid, tok = make()
        t, out = run_in_thread(iid, tok)
        wait_for(lambda: (store.get_item(iid).percent or 0) > 5)
        mgr.cancel_item(iid)
        t.join(10)
        FakeYDL.step_delay = 0
        mgr.retry_item(iid)
        assert runner.run(iid, dispatcher.last_token(iid)) == "completed"
        assert final_file(downloads_dir).stat().st_size == 100 * 100

    def test_cancel_during_processing_discards_the_output(self, downloads_dir):
        FakeYDL.before_finish = lambda: mgr.cancel_item("it1")
        iid, tok = make()
        assert runner.run(iid, tok) == "cancelled"
        assert store.get_item(iid).status is S.CANCELLED
        assert not final_file(downloads_dir).exists()

    def test_deleting_the_record_mid_download_aborts_and_cleans(self, downloads_dir):
        FakeYDL.steps, FakeYDL.step_delay = 200, 0.005
        iid, tok = make()
        t, out = run_in_thread(iid, tok)
        wait_for(lambda: (store.get_item(iid).percent or 0) > 5)
        mgr.delete_job("job1")
        t.join(10)
        assert out["r"] == "removed"
        assert store.get_item(iid) is None and store.get_job("job1") is None  # nothing resurrected
        assert list((downloads_dir / "2026-01-01").glob("*")) == []
        assert slot_limiter.holders() == []


# ───────────────────────── parallelism ─────────────────────────


class TestParallelLimit:
    @pytest.mark.parametrize("limit,n", [(1, 4), (2, 5), (3, 6)])
    def test_never_exceeds_runtime_limit_and_all_complete(self, limit, n):
        runtime_service.update(max_parallel=limit)
        FakeYDL.steps, FakeYDL.step_delay = 8, 0.01
        jobs = make_more(n)
        outs = []
        ts = [threading.Thread(target=lambda a=a: outs.append(runner.run(*a))) for a in jobs]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert outs.count("completed") == n
        assert FakeYDL.max_active <= limit
        assert FakeYDL.max_active == limit  # and the limit is actually used
        assert slot_limiter.holders() == []

    def test_raising_the_limit_while_waiting_admits_workers(self):
        runtime_service.update(max_parallel=1)
        slot_limiter.acquire("blocker:run", 1)
        (iid, tok), = make_more(1)
        t, out = run_in_thread(iid, tok)
        wait_for(lambda: store.get_item(iid).status is S.QUEUED)
        assert t.is_alive()  # waiting
        runtime_service.update(max_parallel=2)
        t.join(5)
        assert out["r"] == "completed"

    def test_dead_slot_holder_does_not_block_the_queue(self, fake_redis):
        runtime_service.update(max_parallel=1)
        slot_limiter.acquire("ghost:run", 1)
        fake_redis.delete(slot_limiter.slot_key("ghost:run"))  # its worker crashed
        iid, tok = make()
        assert runner.run(iid, tok) == "completed"


class TestHeartbeat:
    def test_updated_at_advances_during_silent_phases(self, monkeypatch):
        """No progress events (large check interval) — only the heartbeat keeps the item fresh."""
        monkeypatch.setattr(dl_mod, "_CHECK_INTERVAL", 100.0)
        FakeYDL.steps, FakeYDL.step_delay = 12, 0.1
        iid, tok = make()
        t, _ = run_in_thread(iid, tok)
        wait_for(lambda: store.get_item(iid).status is S.DOWNLOADING)
        first = store.get_item(iid).updated_at
        wait_for(lambda: store.get_item(iid).updated_at > first, timeout=3)
        t.join(10)

    def test_slot_stays_alive_while_running(self, fake_redis):
        FakeYDL.steps, FakeYDL.step_delay = 10, 0.1
        iid, tok = make()
        t, _ = run_in_thread(iid, tok)
        wait_for(lambda: slot_limiter.holders())
        (holder,) = slot_limiter.holders()
        fake_redis.expire(slot_limiter.slot_key(holder), 1)  # would die in 1s…
        wait_for(lambda: fake_redis.ttl(slot_limiter.slot_key(holder)) > 5, timeout=3)  # …heartbeat refreshed it
        t.join(10)
