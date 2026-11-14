"""
LIVE tests: real YouTube, real yt-dlp, real ffmpeg (Redis is still in-memory).

Excluded by default; run with:   uv run pytest -m live -v

They download a few MB into a temp folder and need internet access. Videos used:
    jNQXAC9IVRw   "Me at the zoo" (19 s) — stable, tiny
    dQw4w9WgXcQ   used for the YouTube Music URL form
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

import pytest

from configs import get_ffmpeg_path, get_settings
from models import DownloadRequest, JobKind, PlaylistMode
from models import DownloadStatus as S
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
    metadata_service,
    runtime_service,
    slot_limiter,
)
from tests.fakes import wait_for

pytestmark = pytest.mark.live

ZOO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
MUSIC = "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
PLAYLISTS = [  # tried in order; the first reachable one is used
    "https://www.youtube.com/playlist?list=PL0o_zxa4K1BU6wPPLDsoTj1_wEf0LSNeR",
    "https://www.youtube.com/playlist?list=PL6Rtnh6YJK7ZzKESURdZe82ZWGXEwIrl-",
]


@pytest.fixture(autouse=True)
def live_env(downloads_dir, dispatcher):
    """Temp downloads dir + recording dispatcher; the runner keeps the REAL yt-dlp."""
    return downloads_dir


def ffmpeg_probe(path: Path) -> str:
    """stderr of ``ffmpeg -i`` (stream summary)."""
    out = subprocess.run([get_ffmpeg_path(), "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stderr


def ffmpeg_decodes_cleanly(path: Path) -> bool:
    """Decode the whole file; any corruption makes ffmpeg exit non-zero / print errors."""
    r = subprocess.run([get_ffmpeg_path(), "-v", "error", "-i", str(path), "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0 and not r.stderr.strip()


def run_all(dispatcher, parallel=3):
    """Execute every item the manager enqueued (like Huey workers would)."""
    pending = list(dispatcher.items)
    dispatcher.items.clear()
    outcomes, lock = {}, threading.Lock()
    queue = list(pending)

    def worker():
        while True:
            with lock:
                if not queue:
                    return
                iid, tok, _ = queue.pop(0)
            outcomes[iid] = runner.run(iid, tok)

    ts = [threading.Thread(target=worker) for _ in range(parallel)]
    [t.start() for t in ts]
    [t.join(300) for t in ts]
    return outcomes


def submit_and_run(dispatcher, url, **kw):
    jobs, rejected = mgr.submit(DownloadRequest(urls=[url], **kw))
    assert not rejected
    jid = jobs[0].job_id
    if jobs[0].kind is JobKind.PLAYLIST:
        mgr.resolve_job(jid)
    outcomes = run_all(dispatcher)
    return mgr.get_job_view(jid), outcomes


# ───────────────────────── metadata ─────────────────────────


class TestLiveMetadata:
    def test_preview_single(self):
        m = metadata_service.preview(ZOO)
        assert m.kind is JobKind.SINGLE and "zoo" in m.title.lower() and m.duration == 19
        assert m.available_heights and max(m.available_heights) >= 240
        assert m.thumbnail and m.uploader

    def test_preview_music_url(self):
        m = metadata_service.preview(MUSIC)
        assert m.title and m.duration and 150 < m.duration < 300

    def test_preview_ambiguous_mix_as_playlist(self, monkeypatch):
        monkeypatch.setattr(get_settings().download, "max_playlist_items", 3)
        url = f"{MUSIC}&list=RDAMVMdQw4w9WgXcQ"
        assert metadata_service.preview(url).ambiguous is True
        m = metadata_service.preview(url, PlaylistMode.PLAYLIST)
        assert m.kind is JobKind.PLAYLIST and 1 <= m.entry_count <= 3
        assert all(e.url.startswith("https://music.youtube.com/watch?v=") for e in m.entries)

    def test_search(self):
        res = metadata_service.search("me at the zoo jawed", 5)
        assert res and all(len(r.video_id) == 11 and r.url.startswith("https://www.youtube.com/watch?v=") for r in res)
        assert any(r.video_id == "jNQXAC9IVRw" for r in res), [r.title for r in res]
        assert res[0].title and res[0].thumbnail

    def test_private_or_missing_video_is_a_clean_error(self):
        from services import InvalidRequestError

        with pytest.raises(InvalidRequestError):
            metadata_service.preview("https://www.youtube.com/watch?v=ZZZZZZZZZZZ")


# ───────────────────────── downloads ─────────────────────────


class TestLiveDownloads:
    def test_mp3_with_tags_and_cover(self, live_env):
        from mutagen.id3 import ID3
        from mutagen.mp3 import MP3

        view, out = submit_and_run(dispatcher_of(), ZOO, format="mp3", quality="192")
        item = view.items[0]
        assert item.status is S.COMPLETED, item.error
        f = live_env / item.filepath
        assert f.suffix == ".mp3" and f.stat().st_size > 50_000
        # folder is today's date, file is named after the title
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", Path(item.filepath).parent.name)
        assert "zoo" in f.stem.lower()
        audio = MP3(f)
        assert 17 <= audio.info.length <= 21 and 150_000 <= audio.info.bitrate <= 200_000
        tags = ID3(f)
        assert "zoo" in str(tags.get("TIT2")).lower()
        assert any(k.startswith("APIC") for k in tags.keys()), "cover art was not embedded"
        assert not list(f.parent.glob("*.part")) and not list(f.parent.glob("*.webp")) and not list(f.parent.glob("*.jpg"))

    def test_mp4_360p(self, live_env):
        view, _ = submit_and_run(dispatcher_of(), ZOO, format="mp4", quality="360")
        item = view.items[0]
        assert item.status is S.COMPLETED, item.error
        f = live_env / item.filepath
        probe = ffmpeg_probe(f)
        assert f.suffix == ".mp4" and "Video:" in probe and "Audio:" in probe
        height = int(re.search(r"Video:.*?, (\d+)x(\d+)", probe).group(2))
        assert height <= 360, f"asked for ≤360p, got {height}p"
        assert ffmpeg_decodes_cleanly(f)
        assert not list(f.parent.glob("*.f*.*")) and not list(f.parent.glob("*.part"))  # no leftovers

    @pytest.mark.parametrize("fmt,ext", [("m4a", "m4a"), ("flac", "flac"), ("opus", "opus")])
    def test_other_audio_formats(self, live_env, fmt, ext):
        view, _ = submit_and_run(dispatcher_of(), ZOO, format=fmt)
        item = view.items[0]
        assert item.status is S.COMPLETED, item.error
        f = live_env / item.filepath
        assert f.suffix == f".{ext}" and f.stat().st_size > 20_000 and ffmpeg_decodes_cleanly(f)

    def test_quality_changes_the_bitrate(self, live_env):
        from mutagen.mp3 import MP3

        sizes = {}
        for q in ("64", "320"):
            view, _ = submit_and_run(dispatcher_of(), ZOO, format="mp3", quality=q, folder=f"q{q}")
            assert view.status is S.COMPLETED, view.items[0].error
            sizes[q] = (live_env / view.items[0].filepath).stat().st_size
            assert abs(MP3(live_env / view.items[0].filepath).info.bitrate - int(q) * 1000) < 20_000
        assert sizes["320"] > sizes["64"] * 2

    def test_music_url_downloads_audio(self, live_env):
        view, _ = submit_and_run(dispatcher_of(), MUSIC, format="mp3", quality="128")
        item = view.items[0]
        assert item.status is S.COMPLETED, item.error
        assert (live_env / item.filepath).stat().st_size > 1_000_000

    def test_redownload_skips_existing_file(self, live_env):
        v1, _ = submit_and_run(dispatcher_of(), ZOO, format="mp3", quality="128")
        f = live_env / v1.items[0].filepath
        mtime = f.stat().st_mtime_ns
        v2, _ = submit_and_run(dispatcher_of(), ZOO, format="mp3", quality="128")
        assert v2.items[0].status is S.COMPLETED and v2.items[0].filepath == v1.items[0].filepath
        assert f.stat().st_mtime_ns == mtime  # untouched: yt-dlp saw it was already there
        assert len(list(f.parent.glob("*.mp3"))) == 1


def dispatcher_of():
    from services import dispatch as d

    return d._dispatcher


# ───────────────────────── pause / resume / cancel against the real thing ─────────────────────────


class TestLiveControl:
    @pytest.fixture(autouse=True)
    def throttle(self):
        runtime_service.update(speed_limit=120_000)  # ~120 KB/s so there is time to act
        yield
        runtime_service.update(speed_limit=None)

    def start(self, dispatcher):
        jobs, _ = mgr.submit(DownloadRequest(urls=[ZOO], format="mp4", quality="360"))
        iid, tok, _ = dispatcher.items.pop(0)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", runner.run(iid, tok)))
        t.start()
        wait_for(lambda: (store.get_item(iid).percent or 0) > 3, timeout=60)
        return iid, tok, t, out

    def test_pause_then_resume_finishes_a_valid_file(self, live_env, dispatcher):
        iid, tok, t, out = self.start(dispatcher)
        mgr.pause_item(iid)
        t.join(30)
        assert out["r"] == "paused"
        it = store.get_item(iid)
        assert it.status is S.PAUSED and it.speed is None
        parts = list(live_env.rglob("*.part"))
        assert parts and all(p.stat().st_size > 0 for p in parts), "no partial file kept for resume"
        sizes_before = {p.name: p.stat().st_size for p in parts}

        runtime_service.update(speed_limit=None)
        mgr.resume_item(iid)
        _, new_tok, _ = dispatcher.items.pop(0)
        assert runner.run(iid, new_tok) == "completed"
        it = store.get_item(iid)
        assert it.status is S.COMPLETED and it.attempts == 2
        f = live_env / it.filepath
        assert ffmpeg_decodes_cleanly(f), "resumed download produced a corrupt file"
        assert not list(live_env.rglob("*.part")) and sizes_before

    def test_cancel_leaves_no_files_behind(self, live_env, dispatcher):
        iid, tok, t, out = self.start(dispatcher)
        mgr.cancel_item(iid)
        t.join(30)
        assert out["r"] == "cancelled"
        assert store.get_item(iid).status is S.CANCELLED
        leftovers = [p for p in live_env.rglob("*") if p.is_file()]
        assert leftovers == [], f"cancel left files: {leftovers}"
        assert slot_limiter.holders() == []


# ───────────────────────── playlists ─────────────────────────


class TestLivePlaylist:
    @pytest.fixture
    def picked(self):
        """Choose the two SHORTEST videos of the first reachable playlist."""
        last = None
        for url in PLAYLISTS:
            try:
                info = metadata_service.expand_playlist(url)
            except Exception as exc:  # noqa: BLE001
                last = exc
                continue
            ranked = sorted((e for e in info.entries if e.available and e.duration), key=lambda e: e.duration)[:2]
            if len(ranked) == 2:
                return url, info, sorted(e.index for e in ranked)
        pytest.skip(f"no reachable test playlist ({last})")

    def test_playlist_folder_numbering_and_parallel_download(self, live_env, dispatcher, picked):
        url, info, idx = picked
        runtime_service.update(max_parallel=2)
        view, outcomes = submit_and_run(dispatcher, url, format="mp3", quality="128", playlist_items=",".join(map(str, idx)))
        assert view.kind is JobKind.PLAYLIST and view.status is S.COMPLETED, [i.error for i in view.items]
        assert view.title == info.title and len(view.items) == 2 and list(outcomes.values()) == ["completed"] * 2
        folder = live_env / view.folder
        assert folder.is_dir()
        # folder name = playlist name (sanitised); files are numbered by playlist position
        files = sorted(p.name for p in folder.glob("*.mp3"))
        assert len(files) == 2
        width = max(2, len(str(view.items[0] and len(info.entries))))
        for name, i in zip(files, idx):
            assert name.startswith(f"{i:0{width}d} - "), (name, i)
        assert (live_env / view.folder).name == metadata_service.folder_for_playlist(info.title)

    def test_zip_of_the_downloaded_playlist(self, live_env, dispatcher, picked):
        import io
        import zipfile

        from services import library_service

        url, info, idx = picked
        view, _ = submit_and_run(dispatcher, url, format="mp3", quality="64", playlist_items=",".join(map(str, idx)))
        name, chunks = library_service.zip_folder(view.folder)
        with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as z:
            assert z.testzip() is None and len(z.namelist()) == 2
