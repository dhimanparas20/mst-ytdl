"""Test doubles: a scripted yt-dlp and a recording dispatcher."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from yt_dlp.utils import DownloadError


class FakeDispatcher:
    """Records what the manager enqueues instead of running it."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, int]] = []
        self.resolves: list[str] = []
        self.deliveries: list[str] = []

    def enqueue_item(self, item_id: str, token: str, delay: int = 0) -> None:
        self.items.append((item_id, token, delay))

    def enqueue_delivery(self, job_id: str) -> None:
        self.deliveries.append(job_id)

    def enqueue_resolve(self, job_id: str) -> None:
        self.resolves.append(job_id)

    def last_token(self, item_id: str) -> str | None:
        for iid, tok, _ in reversed(self.items):
            if iid == item_id:
                return tok
        return None


class FakeYDL:
    """
    Scriptable stand-in for ``yt_dlp.YoutubeDL``.

    Class attributes configure behaviour for the next instances:
        steps / step_delay    how many progress ticks, and the pause between them
        fail_with             exception raised mid-download (None = succeed)
        fail_times            fail only the first N process_ie_result calls
        streams               2 simulates video-only + audio-only (merge)
        title / live          extract_info payload
        write_final           create the final file (False simulates a missing file)
    """

    steps = 10
    step_delay = 0.0
    fail_with: BaseException | None = None
    fail_times: int | None = None
    streams = 1
    title = "Fake Song"
    live = False
    write_final = True
    extract_error: BaseException | None = None  # raised by extract_info (e.g. an unavailable video)
    call_post_hook = True
    before_finish: Callable[[], None] | None = None  # runs after the last byte, before post-processing
    probe: Callable[[], None] | None = None  # runs once a post-processor has "started"
    calls = 0
    active = 0  # concurrent process_ie_result calls right now
    max_active = 0
    last_opts: dict[str, Any] = {}
    instances: list["FakeYDL"] = []
    _lock = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        cls.steps, cls.step_delay, cls.fail_with = 10, 0.0, None
        cls.fail_times, cls.streams, cls.title = None, 1, "Fake Song"
        cls.live, cls.write_final, cls.calls = False, True, 0
        cls.call_post_hook, cls.before_finish, cls.probe = True, None, None
        cls.extract_error = None
        cls.active = cls.max_active = 0
        cls.last_opts, cls.instances = {}, []

    def __init__(self, opts: dict[str, Any]):
        self.opts = opts
        FakeYDL.last_opts = opts
        FakeYDL.instances.append(self)
        self.extract_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def _alive(self) -> None:
        """Like the real YoutubeDL: using it after close() silently opens a new, never-closed HTTP session
        (leaked sockets → "Too many open files"), so the fake makes that a hard failure."""
        assert not self.closed, "YoutubeDL used after its `with` block closed it"

    # -- yt-dlp surface used by DownloadRunner --------------------------------

    def extract_info(self, url: str, download: bool = False, process: bool = True) -> dict:
        self._alive()
        self.extract_calls += 1
        if FakeYDL.extract_error is not None:
            raise FakeYDL.extract_error
        return {
            "id": "abc123DEF45",
            "title": self.title,
            "uploader": "Fake Channel",
            "duration": 42,
            "thumbnail": "https://example.test/t.jpg",
            "is_live": self.live,
            "webpage_url": url,
        }

    def prepare_filename(self, info: dict, outtmpl: str | None = None) -> str:
        self._alive()
        if outtmpl is not None:  # explicit template → no extension, like yt-dlp
            name = outtmpl.replace("%(title).150B", info["title"]).replace("%(id)s", info["id"])
            return str(Path(self.opts["paths"]["home"]) / name)
        return str(self._final_path(info, ext="NA"))

    def _final_path(self, info: dict, ext: str | None = None) -> Path:
        home = Path(self.opts["paths"]["home"])
        tmpl = self.opts["outtmpl"]["default"]
        name = tmpl.replace("%(title).150B", info["title"]).replace("%(id)s", info["id"])
        fmt_ext = ext or self._ext()
        return home / name.replace("%(ext)s", fmt_ext)

    def _ext(self) -> str:
        for pp in self.opts.get("postprocessors", []):
            if pp["key"] == "FFmpegExtractAudio":
                return pp["preferredcodec"]
        return self.opts.get("merge_output_format", "mp4")

    def process_ie_result(self, info: dict, download: bool = True) -> dict:
        self._alive()
        with FakeYDL._lock:
            FakeYDL.calls += 1
            FakeYDL.active += 1
            FakeYDL.max_active = max(FakeYDL.max_active, FakeYDL.active)
        try:
            return self._process(info)
        finally:
            with FakeYDL._lock:
                FakeYDL.active -= 1

    def _process(self, info: dict) -> dict:
        final = self._final_path(info)
        final.parent.mkdir(parents=True, exist_ok=True)
        # like yt-dlp: partial/intermediate files live in paths.temp (when set), finals in paths.home
        temp = Path(self.opts["paths"].get("temp") or final.parent)
        temp.mkdir(parents=True, exist_ok=True)
        part = temp / (final.name + ".part")
        # a thumbnail sidecar named after the title — exactly the file two same-title jobs used to fight over
        sidecar = temp / (final.stem + ".webp")
        if self.opts["paths"].get("temp"):
            sidecar.unlink(missing_ok=True)  # private dir: a leftover is our own (pause/resume), overwrite it
        try:
            with sidecar.open("xb") as f:  # shared dir: exclusive — raises if another job already owns it
                f.write(b"thumb")
        except FileExistsError as exc:
            raise PermissionError(f"[WinError 32] file in use: {sidecar}") from exc
        self._sidecar = sidecar
        # resume: continue from whatever a previous (interrupted) run wrote
        start = part.stat().st_size if part.exists() else 0
        progress_hooks = self.opts.get("progress_hooks", [])
        pp_hooks = self.opts.get("postprocessor_hooks", [])
        post_hooks = self.opts.get("post_hooks", [])

        total = self.steps * 100
        ticks = 0
        for stream in range(self.streams):
            stream_info = {
                "acodec": "none" if (self.streams == 2 and stream == 0) else "mp4a",
                "vcodec": "avc1" if (self.streams == 2 and stream == 0) else "none",
            }
            stream_start = start if stream == 0 else 0
            for step in range(stream_start // 100, self.steps):
                time.sleep(self.step_delay)
                with part.open("ab") as f:
                    f.write(b"x" * 100)
                done = (step + 1) * 100
                for h in progress_hooks:
                    h(
                        {
                            "status": "downloading",
                            "downloaded_bytes": done,
                            "total_bytes": total,
                            "speed": 1000.0,
                            "eta": max(self.steps - step - 1, 0),
                            "filename": str(final),
                            "tmpfilename": str(part),
                            "info_dict": stream_info,
                        }
                    )
                ticks += 1
                if FakeYDL.fail_with is not None and ticks == 3:  # fails on the 3rd tick of every call
                    if FakeYDL.fail_times is None or FakeYDL.calls <= FakeYDL.fail_times:
                        raise FakeYDL.fail_with
            for h in progress_hooks:
                h({"status": "finished", "downloaded_bytes": total, "total_bytes": total,
                   "filename": str(final), "info_dict": stream_info})
            start = 0

        if FakeYDL.before_finish:
            FakeYDL.before_finish()
        for h in pp_hooks:
            h({"status": "started", "postprocessor": "FFmpegExtractAudio", "info_dict": info})
        if FakeYDL.probe:
            FakeYDL.probe()
        if self.write_final:
            if part.exists():
                part.replace(final)
            else:
                final.write_bytes(b"x" * total)
            if FakeYDL.call_post_hook:
                for h in post_hooks:
                    h(str(final))
        sidecar.unlink(missing_ok=True)
        for h in pp_hooks:
            h({"status": "finished", "postprocessor": "FFmpegExtractAudio", "info_dict": info})
        return {"requested_downloads": [{"filepath": str(final)}] if self.write_final else []}


def make_download_error(msg: str, expected: bool = False) -> DownloadError:
    """A real yt-dlp DownloadError, optionally wrapping an 'expected' extractor error."""
    from yt_dlp.utils import ExtractorError

    inner = ExtractorError(msg, expected=expected)
    return DownloadError(f"ERROR: {msg}", exc_info=(type(inner), inner, None))


def wait_for(predicate: Callable[[], Any], timeout: float = 5.0, interval: float = 0.02) -> Any:
    """Poll until ``predicate()`` is truthy; raises AssertionError on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")
