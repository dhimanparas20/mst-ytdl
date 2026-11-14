"""Per-download scratch directories: parallel same-title jobs, cleanup rules, hidden from the library."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import services.downloader as dl_mod
from models import DownloadItem, DownloadJob, DownloadOptions
from models import DownloadStatus as S
from models import MediaFormat as F
from modules.ytdlp_util import remove_partial_files
from services import download_manager as mgr
from services import download_runner as runner
from services import job_store as store
from services import library_service as lib
from services import runtime_service
from tests.fakes import FakeYDL, wait_for


@pytest.fixture(autouse=True)
def _env(downloads_dir, fake_ydl, dispatcher, monkeypatch):
    monkeypatch.setattr(dl_mod, "_CHECK_INTERVAL", 0.0)


def add(item_id: str, fmt: F, folder: str = "Same Folder") -> tuple[str, str]:
    job = DownloadJob(job_id=f"j-{item_id}", url="https://www.youtube.com/watch?v=abc123DEF45", folder=folder,
                      options=DownloadOptions(format=fmt), total_items=1)
    store.create_job(job)
    store.add_items([DownloadItem(item_id=item_id, job_id=job.job_id, url=job.url, token=f"t-{item_id}")])
    return item_id, f"t-{item_id}"


class TestParallelSameTitle:
    @pytest.mark.parametrize("formats", [(F.MP3, F.MP4), (F.MP3, F.OPUS), (F.MP3, F.M4A, F.FLAC)])
    def test_same_title_same_folder_different_formats_all_complete(self, downloads_dir, formats):
        """Regression: the thumbnail/intermediate files used to be shared → "file in use" failures."""
        FakeYDL.steps, FakeYDL.step_delay = 12, 0.01
        runtime_service.update(max_parallel=len(formats))
        jobs = [add(f"it{n}", fmt) for n, fmt in enumerate(formats)]
        outs: dict[str, str] = {}
        ts = [threading.Thread(target=lambda a=a: outs.__setitem__(a[0], runner.run(*a))) for a in jobs]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert set(outs.values()) == {"completed"}, {i: store.get_item(i).error for i, _ in jobs}
        assert FakeYDL.max_active == len(formats)  # they really did overlap
        files = sorted(p.suffix for p in (downloads_dir / "Same Folder").iterdir())
        assert files == sorted(f".{fmt.value}" for fmt in formats)

    def test_sharing_a_directory_would_have_collided(self, downloads_dir):
        """Proves the fake models the old failure: without a private temp dir the second job dies."""
        FakeYDL.steps, FakeYDL.step_delay = 12, 0.02
        errors: list[str] = []
        shared = downloads_dir / "shared"
        shared.mkdir()

        def go(fmt):
            opts = {"paths": {"home": str(shared)}, "outtmpl": {"default": "%(title).150B.%(ext)s"},
                    "merge_output_format": fmt, "postprocessors": []}
            try:
                FakeYDL(opts).process_ie_result({"title": "Fake Song", "id": "abc123DEF45"})
            except PermissionError as exc:
                errors.append(str(exc))

        ts = [threading.Thread(target=go, args=(f,)) for f in ("mp4", "mkv")]
        [t.start() for t in ts]
        [t.join(15) for t in ts]
        assert len(errors) == 1 and "file in use" in errors[0]


class TestScratchLifecycle:
    def test_success_removes_the_scratch_dir_and_leaves_only_the_file(self, downloads_dir):
        iid, tok = add("it1", F.MP3, "Out")
        assert runner.run(iid, tok) == "completed"
        assert not (downloads_dir / ".tmp" / "it1").exists()
        assert [p.name for p in (downloads_dir / "Out").iterdir()] == ["Fake Song.mp3"]

    def test_pause_keeps_scratch_and_remembers_it(self, downloads_dir):
        FakeYDL.steps, FakeYDL.step_delay = 200, 0.005
        iid, tok = add("it1", F.MP3, "Out")
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", runner.run(iid, tok)))
        t.start()
        wait_for(lambda: (store.get_item(iid).percent or 0) > 10)
        mgr.pause_item(iid)
        t.join(10)
        assert out["r"] == "paused"
        scratch = downloads_dir / ".tmp" / "it1"
        assert scratch.is_dir() and any(scratch.glob("*.part"))
        assert store.get_item(iid).partials == [str(scratch)]
        assert not list((downloads_dir / "Out").glob("*"))  # nothing half-written in the destination

    def test_cancel_removes_the_whole_scratch_dir(self, downloads_dir):
        FakeYDL.steps, FakeYDL.step_delay = 200, 0.005
        iid, tok = add("it1", F.MP3, "Out")
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", runner.run(iid, tok)))
        t.start()
        wait_for(lambda: (store.get_item(iid).percent or 0) > 5)
        mgr.cancel_item(iid)
        t.join(10)
        assert out["r"] == "cancelled" and not (downloads_dir / ".tmp" / "it1").exists()

    def test_cancelling_a_paused_item_removes_scratch(self, downloads_dir):
        FakeYDL.steps, FakeYDL.step_delay = 200, 0.005
        iid, tok = add("it1", F.MP3, "Out")
        t = threading.Thread(target=lambda: runner.run(iid, tok))
        t.start()
        wait_for(lambda: (store.get_item(iid).percent or 0) > 5)
        mgr.pause_item(iid)
        t.join(10)
        assert (downloads_dir / ".tmp" / "it1").is_dir()
        mgr.cancel_item(iid)
        assert not (downloads_dir / ".tmp" / "it1").exists()

    def test_restart_discards_the_partial_scratch(self, downloads_dir):
        FakeYDL.steps, FakeYDL.step_delay = 200, 0.005
        iid, tok = add("it1", F.MP3, "Out")
        t = threading.Thread(target=lambda: runner.run(iid, tok))
        t.start()
        wait_for(lambda: (store.get_item(iid).percent or 0) > 5)
        mgr.pause_item(iid)
        t.join(10)
        mgr.restart_item(iid)
        assert not (downloads_dir / ".tmp" / "it1").exists()

    def test_failure_keeps_scratch_so_a_retry_can_resume(self, downloads_dir):
        from tests.fakes import make_download_error

        FakeYDL.fail_with = make_download_error("Private video")
        iid, tok = add("it1", F.MP3, "Out")
        assert runner.run(iid, tok) == "failed"
        assert (downloads_dir / ".tmp" / "it1").is_dir()
        assert store.get_item(iid).status is S.FAILED

    def test_yt_dlp_receives_the_temp_path(self, downloads_dir):
        iid, tok = add("it1", F.MP3, "Out")
        runner.run(iid, tok)
        paths = FakeYDL.last_opts["paths"]
        assert Path(paths["home"]) == downloads_dir / "Out"
        assert Path(paths["temp"]) == downloads_dir / ".tmp" / "it1"


class TestNoEmptyFolders:
    def test_unavailable_video_leaves_no_destination_folder(self, downloads_dir):
        from tests.fakes import make_download_error

        FakeYDL.extract_error = make_download_error("[youtube] x: Video unavailable")
        iid, tok = add("it1", F.MP3, "Would Be Created")
        assert runner.run(iid, tok) == "failed"
        assert not (downloads_dir / "Would Be Created").exists()
        assert [e.name for e in lib.list_dir("")] == []


class TestHiddenFromLibrary:
    def test_dot_folders_never_show_up(self, downloads_dir):
        (downloads_dir / ".tmp" / "it1").mkdir(parents=True)
        (downloads_dir / ".tmp" / "it1" / "intermediate.webm").write_bytes(b"x" * 100)
        (downloads_dir / "Real").mkdir()
        (downloads_dir / "Real" / "song.mp3").write_bytes(b"y" * 10)
        assert [e.name for e in lib.list_dir("")] == ["Real"]
        assert lib.usage() == {"files": 1, "bytes": 10}
        assert lib.list_folders() == ["Real"]
        _, chunks = lib.zip_paths(["Real", ".tmp"])
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as z:
            assert z.namelist() == ["Real/song.mp3"]


class TestRemovePartialDirectories:
    def test_directory_is_removed_with_contents_and_counted(self, tmp_path):
        d = tmp_path / "scratch"
        (d / "sub").mkdir(parents=True)
        (d / "a.part").write_bytes(b"1")
        (d / "sub" / "b.webm").write_bytes(b"2")
        assert remove_partial_files([str(d)]) == 2
        assert not d.exists()

    def test_mixed_files_and_dirs(self, tmp_path):
        d = tmp_path / "scratch"
        d.mkdir()
        (d / "x").write_bytes(b"1")
        f = tmp_path / "song.mp3"
        Path(str(f) + ".part").write_bytes(b"1")
        assert remove_partial_files([str(d), str(f)]) == 2
