"""services/manager.py — submit, resolve, controls, bulk ops, removal, reads, reconcile."""

from __future__ import annotations

import time

import pytest

from models import (
    DownloadRequest,
    EntryInfo,
    JobKind,
)
from models import (
    DownloadStatus as S,
)
from models import (
    MediaFormat as F,
)
from models import (
    PlaylistMode as PM,
)
from models import (
    ResolveState as R,
)
from services import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    metadata_service,
    slot_limiter,
)
from services import (
    download_manager as mgr,
)
from services import (
    job_store as store,
)
from services.metadata import PlaylistInfo

VID = "dQw4w9WgXcQ"
VID2 = "9bZkp7q19f0"
PL = "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"
WATCH = f"https://www.youtube.com/watch?v={VID}"
PLAYLIST = f"https://www.youtube.com/playlist?list={PL}"


def req(*urls, **kw) -> DownloadRequest:
    return DownloadRequest(urls=list(urls) or [WATCH], **kw)


def entries(n, bad=()):
    out = []
    for i in range(1, n + 1):
        vid = f"{i:011d}"
        out.append(EntryInfo(index=i, video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
                             title=f"Song {i}", duration=100 + i, thumbnail=f"http://t/{i}", available=i not in bad))
    return out


@pytest.fixture
def playlist(monkeypatch):
    """Control what expand_playlist returns."""
    state = {"info": PlaylistInfo("My Mix", "DJ", "http://t/cover", entries(4)), "error": None}

    def fake(url):
        if state["error"]:
            raise state["error"]
        return state["info"]

    monkeypatch.setattr(metadata_service, "expand_playlist", fake)
    return state


def make_playlist_job(dispatcher, playlist, **kw):
    jobs, _ = mgr.submit(req(PLAYLIST, **kw))
    mgr.resolve_job(jobs[0].job_id)
    return jobs[0].job_id


# ───────────────────────── options ─────────────────────────


class TestResolveOptions:
    def test_defaults_come_from_settings(self):
        o = mgr.resolve_options(req())
        assert o.format is F.MP3 and o.quality == "192" and o.embed_metadata and o.embed_thumbnail

    def test_video_default_quality(self):
        assert mgr.resolve_options(req(format="mp4")).quality == "1080"

    def test_explicit_values_win(self):
        o = mgr.resolve_options(req(format="mp4", quality="720", embed_metadata=False, embed_thumbnail=False, subtitles=True, speed_limit=5))
        assert (o.quality, o.embed_metadata, o.embed_thumbnail, o.subtitles, o.speed_limit) == ("720", False, False, True, 5)

    def test_sponsorblock_defaults_off_and_is_passed_through(self):
        assert mgr.resolve_options(req()).sponsorblock is False
        assert mgr.resolve_options(req(sponsorblock=True)).sponsorblock is True

    def test_overwrite_defaults_off_and_is_passed_through(self):
        assert mgr.resolve_options(req()).overwrite is False
        assert mgr.resolve_options(req(overwrite=True)).overwrite is True

    def test_lossless_defaults_to_best(self):
        assert mgr.resolve_options(req(format="flac")).quality == "best"

    @pytest.mark.parametrize("fmt,q", [("mp3", "1080"), ("mp4", "320"), ("mp3", "abc")])
    def test_bad_combinations_are_user_errors(self, fmt, q):
        with pytest.raises(InvalidRequestError):
            mgr.resolve_options(req(format=fmt, quality=q))


# ───────────────────────── submit ─────────────────────────


class TestSubmitSingle:
    def test_creates_job_item_and_enqueues(self, dispatcher):
        jobs, rejected = mgr.submit(req(WATCH, format="mp3", quality="320"))
        assert rejected == [] and len(jobs) == 1
        v = jobs[0]
        assert v.kind is JobKind.SINGLE and v.status is S.QUEUED and v.total_items == 1
        assert v.options.format is F.MP3 and v.options.quality == "320"
        assert v.url == WATCH and v.folder  # date folder
        (it,) = v.items
        assert it.status is S.QUEUED and it.video_id == VID and it.token
        assert dispatcher.items == [(it.item_id, it.token, 0)] and dispatcher.resolves == []

    def test_url_is_canonicalised(self, dispatcher):
        jobs, _ = mgr.submit(req(f"{WATCH}&t=30&list={PL}&index=4"))
        assert jobs[0].url == WATCH and jobs[0].kind is JobKind.SINGLE

    def test_folder_is_today(self, dispatcher):
        from configs import get_settings
        from modules import today_str

        dl = get_settings().download
        jobs, _ = mgr.submit(req())
        assert jobs[0].folder == today_str(dl.timezone, dl.date_folder_format)

    def test_folder_override(self, dispatcher):
        assert mgr.submit(req(folder="Road Trip"))[0][0].folder == "Road Trip"

    def test_multiple_urls_make_multiple_jobs(self, dispatcher):
        jobs, _ = mgr.submit(req(WATCH, f"https://youtu.be/{VID2}"))
        assert len(jobs) == 2 and len(dispatcher.items) == 2
        assert {j.items[0].video_id for j in jobs} == {VID, VID2}

    def test_music_url(self, dispatcher):
        jobs, _ = mgr.submit(req(f"https://music.youtube.com/watch?v={VID}"))
        assert jobs[0].url.startswith("https://music.youtube.com/")


class TestSubmitPlaylist:
    def test_enqueues_resolve_not_items(self, dispatcher):
        jobs, _ = mgr.submit(req(PLAYLIST))
        v = jobs[0]
        assert v.kind is JobKind.PLAYLIST and v.status is S.RESOLVING and v.resolve_state is R.PENDING
        assert dispatcher.resolves == [v.job_id] and dispatcher.items == []
        assert store.get_job(v.job_id).url == PLAYLIST

    def test_ambiguous_url_defaults_single_and_can_force_playlist(self, dispatcher):
        both = f"{WATCH}&list={PL}"
        assert mgr.submit(req(both))[0][0].kind is JobKind.SINGLE
        assert mgr.submit(req(both, playlist_mode=PM.PLAYLIST))[0][0].kind is JobKind.PLAYLIST

    def test_selection_and_folder_stored_on_job(self, dispatcher):
        jid = mgr.submit(req(PLAYLIST, playlist_items="1,3", folder="Faves"))[0][0].job_id
        j = store.get_job(jid)
        assert (j.playlist_items, j.folder_override) == ("1,3", "Faves")

    def test_bad_selection_syntax_is_rejected_per_url(self, dispatcher):
        with pytest.raises(InvalidRequestError, match="selection"):
            mgr.submit(req(PLAYLIST, playlist_items="abc"))


class TestSubmitValidation:
    def test_bad_urls_are_reported_good_ones_proceed(self, dispatcher):
        jobs, rejected = mgr.submit(req(WATCH, "https://vimeo.com/1", "garbage"))
        assert len(jobs) == 1
        assert [r["url"] for r in rejected] == ["https://vimeo.com/1", "garbage"]
        assert all(r["error"] for r in rejected)

    def test_all_bad_raises(self, dispatcher):
        with pytest.raises(InvalidRequestError):
            mgr.submit(req("https://vimeo.com/1"))
        assert dispatcher.items == [] and store.count_jobs() == 0

    def test_all_bad_error_lists_every_reason(self, dispatcher):
        with pytest.raises(InvalidRequestError) as e:
            mgr.submit(req("https://vimeo.com/1", "nonsense"))
        assert ";" in str(e.value)

    def test_invalid_options_create_nothing(self, dispatcher):
        with pytest.raises(InvalidRequestError):
            mgr.submit(req(format="mp3", quality="1080"))
        assert store.count_jobs() == 0


# ───────────────────────── resolve ─────────────────────────


class TestResolve:
    def test_creates_items_and_enqueues_each(self, dispatcher, playlist):
        jid = make_playlist_job(dispatcher, playlist)
        j = store.get_job(jid)
        assert j.resolve_state is R.READY and j.title == "My Mix" and j.uploader == "DJ"
        assert j.folder == "My Mix" and j.total_items == 4 and j.playlist_size == 4
        items = store.get_items(jid)
        assert [i.index for i in items] == [1, 2, 3, 4] and all(i.status is S.QUEUED for i in items)
        assert [i.title for i in items] == [f"Song {n}" for n in range(1, 5)]
        assert {iid for iid, _, _ in dispatcher.items} == {i.item_id for i in items}
        assert all(i.token for i in items)

    def test_selection_keeps_original_indices(self, dispatcher, playlist):
        jid = make_playlist_job(dispatcher, playlist, playlist_items="2,4")
        items = store.get_items(jid)
        assert [i.index for i in items] == [2, 4]
        j = store.get_job(jid)
        assert j.total_items == 2 and j.playlist_size == 4  # numbering width uses the full size

    def test_unavailable_entries_become_failed_and_not_enqueued(self, dispatcher, playlist):
        playlist["info"] = PlaylistInfo("Mix", None, None, entries(3, bad={2}))
        jid = make_playlist_job(dispatcher, playlist)
        items = {i.index: i for i in store.get_items(jid)}
        assert items[2].status is S.FAILED and "Unavailable" in items[2].error and not items[2].token
        assert len(dispatcher.items) == 2 and items[2].item_id not in {i for i, _, _ in dispatcher.items}

    def test_folder_override_and_sanitising(self, dispatcher, playlist):
        playlist["info"] = PlaylistInfo('AC/DC: "Hits"', None, None, entries(1))
        assert store.get_job(make_playlist_job(dispatcher, playlist)).folder == "ACDC Hits"
        playlist["info"] = PlaylistInfo("Orig", None, None, entries(1))
        assert store.get_job(make_playlist_job(dispatcher, playlist, folder="Mine")).folder == "Mine"

    def test_expansion_failure_marks_job_failed(self, dispatcher, playlist):
        from tests.fakes import make_download_error

        playlist["error"] = make_download_error("[youtube:tab] Private playlist")
        jid = make_playlist_job(dispatcher, playlist)
        j = store.get_job(jid)
        assert j.resolve_state is R.FAILED and "Private playlist" in j.resolve_error
        assert dispatcher.items == [] and mgr.get_job_view(jid).status is S.FAILED

    def test_empty_playlist_fails(self, dispatcher, playlist):
        playlist["info"] = PlaylistInfo("Empty", None, None, [])
        jid = make_playlist_job(dispatcher, playlist)
        assert store.get_job(jid).resolve_state is R.FAILED and "empty" in store.get_job(jid).resolve_error

    def test_selection_matching_nothing_fails(self, dispatcher, playlist):
        jid = make_playlist_job(dispatcher, playlist, playlist_items="99")
        assert store.get_job(jid).resolve_state is R.FAILED

    def test_second_resolve_is_a_noop(self, dispatcher, playlist):
        jid = make_playlist_job(dispatcher, playlist)
        n = len(dispatcher.items)
        mgr.resolve_job(jid)  # duplicate task delivery
        assert len(dispatcher.items) == n and len(store.get_items(jid)) == 4

    def test_resolve_unknown_job_is_a_noop(self, dispatcher):
        mgr.resolve_job("ghost")

    def test_cancel_while_pending_stops_resolution(self, dispatcher, playlist):
        jid = mgr.submit(req(PLAYLIST))[0][0].job_id
        mgr.cancel_job(jid)
        mgr.resolve_job(jid)  # task fires after the cancel
        assert store.get_items(jid) == [] and dispatcher.items == []
        assert store.get_job(jid).resolve_error == "Cancelled"

    def test_cancel_during_expansion_discards_results(self, dispatcher, monkeypatch):
        jid = mgr.submit(req(PLAYLIST))[0][0].job_id

        def slow_expand(url):
            mgr.cancel_job(jid)  # user hits cancel while yt-dlp is working
            return PlaylistInfo("Mix", None, None, entries(3))

        monkeypatch.setattr(metadata_service, "expand_playlist", slow_expand)
        mgr.resolve_job(jid)
        assert store.get_items(jid) == [] and dispatcher.items == []

    def test_retry_failed_resolution_reenqueues_resolve(self, dispatcher, playlist):
        playlist["error"] = RuntimeError("net down")
        jid = make_playlist_job(dispatcher, playlist)
        assert store.get_job(jid).resolve_state is R.FAILED
        dispatcher.resolves.clear()
        playlist["error"] = None
        mgr.retry_job(jid)
        assert dispatcher.resolves == [jid] and store.get_job(jid).resolve_state is R.PENDING
        mgr.resolve_job(jid)
        assert len(store.get_items(jid)) == 4


# ───────────────────────── item controls ─────────────────────────


def single(dispatcher, **kw):
    v = mgr.submit(req(**kw))[0][0]
    return v.job_id, v.items[0].item_id


def force(item_id, **fields):
    store.patch_item(item_id, **fields)


class TestPause:
    def test_queued_pauses_immediately_and_invalidates_token(self, dispatcher):
        _, iid = single(dispatcher)
        it = mgr.pause_item(iid)
        assert it.status is S.PAUSED and it.token == "" and it.control is None

    def test_running_gets_a_pause_request(self, dispatcher):
        _, iid = single(dispatcher)
        force(iid, status=S.DOWNLOADING)
        it = mgr.pause_item(iid)
        assert it.status is S.DOWNLOADING and it.control.value == "pause"

    def test_processing_gets_a_pause_request(self, dispatcher):
        _, iid = single(dispatcher)
        force(iid, status=S.PROCESSING)
        assert mgr.pause_item(iid).control.value == "pause"

    @pytest.mark.parametrize("status", [S.COMPLETED, S.FAILED, S.CANCELLED, S.PAUSED])
    def test_other_states_conflict(self, dispatcher, status):
        _, iid = single(dispatcher)
        force(iid, status=status)
        with pytest.raises(ConflictError):
            mgr.pause_item(iid)

    def test_unknown_item(self):
        with pytest.raises(NotFoundError):
            mgr.pause_item("ghost")


class TestResume:
    def test_paused_requeues_with_fresh_token(self, dispatcher):
        _, iid = single(dispatcher)
        old = dispatcher.last_token(iid)
        mgr.pause_item(iid)
        it = mgr.resume_item(iid)
        assert it.status is S.QUEUED and it.token and it.token != old
        assert dispatcher.last_token(iid) == it.token and len(dispatcher.items) == 2

    def test_resume_keeps_attempt_count_retry_resets_it(self, dispatcher):
        _, iid = single(dispatcher)
        force(iid, status=S.PAUSED, attempts=2)
        assert mgr.resume_item(iid).attempts == 2
        force(iid, status=S.FAILED, attempts=2)
        assert mgr.retry_item(iid).attempts == 0

    @pytest.mark.parametrize("status", [S.QUEUED, S.DOWNLOADING, S.COMPLETED, S.FAILED, S.CANCELLED])
    def test_only_paused_can_resume(self, dispatcher, status):
        _, iid = single(dispatcher)
        force(iid, status=status)
        with pytest.raises(ConflictError):
            mgr.resume_item(iid)

    def test_resume_clears_error(self, dispatcher):
        _, iid = single(dispatcher)
        force(iid, status=S.PAUSED, error="Interrupted")
        assert mgr.resume_item(iid).error is None

    def test_conflict_does_not_enqueue(self, dispatcher):
        _, iid = single(dispatcher)
        n = len(dispatcher.items)
        with pytest.raises(ConflictError):
            mgr.resume_item(iid)
        assert len(dispatcher.items) == n


class TestCancel:
    def test_queued_cancels_immediately(self, dispatcher):
        _, iid = single(dispatcher)
        it = mgr.cancel_item(iid)
        assert it.status is S.CANCELLED and it.token == "" and it.finished_at and it.percent == 0

    def test_paused_cancel_deletes_partial_files(self, dispatcher, tmp_path):
        _, iid = single(dispatcher)
        part = tmp_path / "x.mp3.part"
        part.write_bytes(b"data")
        force(iid, status=S.PAUSED, partials=[str(tmp_path / "x.mp3")])
        it = mgr.cancel_item(iid)
        assert it.status is S.CANCELLED and not part.exists() and it.partials == []

    def test_running_gets_cancel_request(self, dispatcher):
        _, iid = single(dispatcher)
        force(iid, status=S.DOWNLOADING)
        assert mgr.cancel_item(iid).control.value == "cancel"

    @pytest.mark.parametrize("status", [S.COMPLETED, S.FAILED, S.CANCELLED])
    def test_terminal_states_conflict(self, dispatcher, status):
        _, iid = single(dispatcher)
        force(iid, status=status)
        with pytest.raises(ConflictError):
            mgr.cancel_item(iid)


class TestRetry:
    @pytest.mark.parametrize("status", [S.FAILED, S.CANCELLED])
    def test_failed_and_cancelled_can_retry(self, dispatcher, status):
        _, iid = single(dispatcher)
        force(iid, status=status, error="boom", attempts=3)
        it = mgr.retry_item(iid)
        assert it.status is S.QUEUED and it.error is None and it.attempts == 0 and it.finished_at is None

    @pytest.mark.parametrize("status", [S.QUEUED, S.DOWNLOADING, S.PAUSED, S.COMPLETED])
    def test_others_conflict(self, dispatcher, status):
        _, iid = single(dispatcher)
        force(iid, status=status)
        with pytest.raises(ConflictError):
            mgr.retry_item(iid)


# ───────────────────────── job + bulk ─────────────────────────


class TestJobAndBulk:
    def test_pause_and_resume_job(self, dispatcher, playlist):
        jid = make_playlist_job(dispatcher, playlist)
        ids = store.get_item_ids(jid)
        force(ids[0], status=S.COMPLETED)
        force(ids[1], status=S.DOWNLOADING)
        v = mgr.pause_job(jid)
        by = {i.index: i for i in v.items}
        assert by[1].status is S.COMPLETED  # untouched
        assert by[2].control.value == "pause"  # running → request
        assert by[3].status is S.PAUSED and by[4].status is S.PAUSED
        v = mgr.resume_job(jid)
        assert {i.index: i.status for i in v.items}[3] is S.QUEUED

    def test_cancel_job_cancels_unfinished_only(self, dispatcher, playlist):
        jid = make_playlist_job(dispatcher, playlist)
        ids = store.get_item_ids(jid)
        force(ids[0], status=S.COMPLETED)
        v = mgr.cancel_job(jid)
        st = [i.status for i in v.items]
        assert st == [S.COMPLETED, S.CANCELLED, S.CANCELLED, S.CANCELLED] and v.status is S.PARTIAL

    def test_retry_job_requeues_failed_and_cancelled(self, dispatcher, playlist):
        jid = make_playlist_job(dispatcher, playlist)
        ids = store.get_item_ids(jid)
        force(ids[0], status=S.COMPLETED)
        force(ids[1], status=S.FAILED)
        force(ids[2], status=S.CANCELLED)
        force(ids[3], status=S.PAUSED)
        v = mgr.retry_job(jid)
        assert [i.status for i in v.items] == [S.COMPLETED, S.QUEUED, S.QUEUED, S.PAUSED]

    def test_unknown_job(self):
        for fn in (mgr.pause_job, mgr.resume_job, mgr.cancel_job, mgr.retry_job, mgr.get_job_view):
            with pytest.raises(NotFoundError):
                fn("ghost")

    def test_pause_all_resume_all(self, dispatcher):
        ids = [single(dispatcher)[1] for _ in range(3)]
        force(ids[2], status=S.COMPLETED)
        assert mgr.pause_all() == 2
        assert store.get_item(ids[0]).status is S.PAUSED and store.get_item(ids[2]).status is S.COMPLETED
        assert mgr.resume_all() == 2
        assert store.get_item(ids[0]).status is S.QUEUED

    def test_cancel_all_also_aborts_resolving_jobs(self, dispatcher):
        iid = single(dispatcher)[1]
        pjob = mgr.submit(req(PLAYLIST))[0][0].job_id
        assert mgr.cancel_all() == 1
        assert store.get_item(iid).status is S.CANCELLED
        assert store.get_job(pjob).resolve_state is R.FAILED

    def test_retry_failed_skips_cancelled(self, dispatcher):
        a, b, c = (single(dispatcher)[1] for _ in range(3))
        force(a, status=S.FAILED)
        force(b, status=S.CANCELLED)
        force(c, status=S.COMPLETED)
        assert mgr.retry_failed() == 1
        assert store.get_item(a).status is S.QUEUED and store.get_item(b).status is S.CANCELLED


# ───────────────────────── removal ─────────────────────────


class TestDelete:
    def test_delete_job_removes_records_only_by_default(self, dispatcher, downloads_dir):
        jid, iid = single(dispatcher)
        f = downloads_dir / "d" / "a.mp3"
        f.parent.mkdir()
        f.write_bytes(b"x")
        force(iid, status=S.COMPLETED, filepath="d/a.mp3")
        assert mgr.delete_job(jid) == {"items": 1, "files": 0}
        assert f.exists() and store.get_job(jid) is None and store.get_item(iid) is None

    def test_delete_with_files(self, dispatcher, downloads_dir):
        jid, iid = single(dispatcher)
        folder = store.get_job(jid).folder
        f = downloads_dir / folder / "a.mp3"
        f.parent.mkdir()
        f.write_bytes(b"x")
        force(iid, status=S.COMPLETED, filepath=f"{folder}/a.mp3")
        assert mgr.delete_job(jid, delete_files=True)["files"] == 1
        assert not f.exists() and not f.parent.exists()  # emptied folder removed

    def test_folder_kept_when_it_still_holds_other_files(self, dispatcher, downloads_dir):
        jid, iid = single(dispatcher)
        folder = store.get_job(jid).folder
        (downloads_dir / folder).mkdir()
        (downloads_dir / folder / "a.mp3").write_bytes(b"x")
        (downloads_dir / folder / "other.mp3").write_bytes(b"y")
        force(iid, status=S.COMPLETED, filepath=f"{folder}/a.mp3")
        mgr.delete_job(jid, delete_files=True)
        assert (downloads_dir / folder / "other.mp3").exists()

    def test_traversal_in_recorded_path_is_not_followed(self, dispatcher, downloads_dir, tmp_path):
        jid, iid = single(dispatcher)
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me")
        force(iid, status=S.COMPLETED, filepath="../victim.txt")
        mgr.delete_job(jid, delete_files=True)
        assert victim.exists()

    def test_delete_removes_partials(self, dispatcher, downloads_dir):
        jid, iid = single(dispatcher)
        part = downloads_dir / "a.mp3.part"
        part.write_bytes(b"x")
        force(iid, status=S.PAUSED, partials=[str(downloads_dir / "a.mp3")])
        mgr.delete_job(jid, delete_files=True)
        assert not part.exists()

    def test_delete_unknown(self):
        with pytest.raises(NotFoundError):
            mgr.delete_job("ghost")

    def test_clear_finished_only_removes_terminal_jobs(self, dispatcher):
        done = single(dispatcher)
        failed = single(dispatcher)
        live = single(dispatcher)
        force(done[1], status=S.COMPLETED)
        force(failed[1], status=S.FAILED)
        assert mgr.clear_finished() == 2
        assert store.get_job(live[0]) is not None and store.get_job(done[0]) is None


# ───────────────────────── reads ─────────────────────────


class TestReads:
    def test_get_job_view_includes_items(self, dispatcher):
        jid, _ = single(dispatcher)
        v = mgr.get_job_view(jid)
        assert v.items and v.items[0].job_id == jid
        assert mgr.get_job_view(jid, include_items=False).items is None

    def test_get_item(self, dispatcher):
        _, iid = single(dispatcher)
        assert mgr.get_item(iid).item_id == iid
        with pytest.raises(NotFoundError):
            mgr.get_item("ghost")

    def test_list_is_newest_first_with_pagination(self, dispatcher):
        ids = []
        for _ in range(4):
            ids.append(single(dispatcher)[0])
            time.sleep(0.005)
        listed = [v.job_id for v in mgr.list_job_views()]
        assert listed == ids[::-1]
        assert [v.job_id for v in mgr.list_job_views(limit=2, offset=1)] == ids[::-1][1:3]

    def test_list_status_filter(self, dispatcher):
        a, b = single(dispatcher), single(dispatcher)
        force(a[1], status=S.COMPLETED)
        done = mgr.list_job_views(status=S.COMPLETED)
        assert [v.job_id for v in done] == [a[0]]
        assert [v.job_id for v in mgr.list_job_views(status=S.QUEUED)] == [b[0]]
        assert mgr.list_job_views(status=S.FAILED) == []

    def test_list_include_items(self, dispatcher):
        single(dispatcher)
        assert mgr.list_job_views()[0].items is None
        assert mgr.list_job_views(include_items=True)[0].items

    def test_snapshot_embeds_items_for_unfinished_jobs_and_all_singles(self, dispatcher, playlist):
        live, done = single(dispatcher), single(dispatcher)
        force(done[1], status=S.COMPLETED)
        pl_done = make_playlist_job(dispatcher, playlist)
        for iid in store.get_item_ids(pl_done):
            force(iid, status=S.COMPLETED)
        pl_live = make_playlist_job(dispatcher, playlist)
        views, stats = mgr.snapshot()
        by = {v.job_id: v for v in views}
        assert by[live[0]].items is not None
        assert by[done[0]].items is not None  # finished single: UI needs filepath / error
        assert by[pl_live].items is not None  # unfinished playlist
        assert by[pl_done].items is None  # finished playlist: fetched on demand
        assert stats.total_jobs == 4

    def test_snapshot_stats_roll_up(self, dispatcher):
        single(dispatcher)
        done = single(dispatcher)
        force(done[1], status=S.COMPLETED)
        _, stats = mgr.snapshot()
        assert stats.queued == 1 and stats.completed == 1 and stats.total_jobs == 2

    def test_stats(self, dispatcher):
        ids = [single(dispatcher)[1] for _ in range(6)]
        for iid, st, sp in zip(ids, [S.DOWNLOADING, S.DOWNLOADING, S.PAUSED, S.COMPLETED, S.FAILED, S.CANCELLED],
                               [1000.0, 500.0, None, None, None, None]):
            force(iid, status=st, speed=sp, downloaded_bytes=10)
        s = mgr.stats()
        assert (s.active, s.paused, s.completed, s.failed, s.cancelled, s.queued) == (2, 1, 1, 1, 1, 0)
        assert s.speed == 1500.0 and s.downloaded_bytes == 60 and s.total_items == 6
        assert s.max_parallel >= 1 and s.workers >= 1


# ───────────────────────── reconcile ─────────────────────────


class TestReconcile:
    def test_dead_running_item_becomes_paused(self, dispatcher):
        _, iid = single(dispatcher)
        force(iid, status=S.DOWNLOADING)
        store.r.hset(store.item_key(iid), "updated_at", str(time.time() - 120))
        assert mgr.reconcile() == 1
        it = store.get_item(iid)
        assert it.status is S.PAUSED and "Interrupted" in it.error and it.token == ""
        assert mgr.resume_item(iid).status is S.QUEUED  # user can recover it

    def test_item_with_live_slot_is_left_alone(self, dispatcher):
        _, iid = single(dispatcher)
        force(iid, status=S.DOWNLOADING)
        slot_limiter.acquire(f"{iid}:run1", 3)  # holders are "<item>:<run>"
        store.r.hset(store.item_key(iid), "updated_at", str(time.time() - 120))
        assert mgr.reconcile() == 0 and store.get_item(iid).status is S.DOWNLOADING

    def test_recently_updated_item_is_left_alone(self, dispatcher):
        _, iid = single(dispatcher)
        force(iid, status=S.PROCESSING)
        assert mgr.reconcile() == 0

    def test_queued_and_terminal_items_untouched(self, dispatcher):
        a, b = single(dispatcher)[1], single(dispatcher)[1]
        force(b, status=S.COMPLETED)
        for iid in (a, b):
            store.r.hset(store.item_key(iid), "updated_at", str(time.time() - 9999))
        assert mgr.reconcile() == 0

    def test_stuck_resolve_times_out(self, dispatcher):
        jid = mgr.submit(req(PLAYLIST))[0][0].job_id
        store.r.hset(store.job_key(jid), "updated_at", str(time.time() - 3600))
        assert mgr.reconcile() == 1
        j = store.get_job(jid)
        assert j.resolve_state is R.FAILED and "Timed out" in j.resolve_error

    def test_fresh_resolve_not_timed_out(self, dispatcher):
        mgr.submit(req(PLAYLIST))
        assert mgr.reconcile() == 0
