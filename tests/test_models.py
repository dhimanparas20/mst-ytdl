"""models/: validation rules, status derivation, view aggregation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models import (
    DownloadItem,
    DownloadJob,
    DownloadOptions,
    DownloadRequest,
    JobKind,
    JobView,
    derive_job_status,
)
from models import (
    DownloadStatus as S,
)
from models import (
    MediaFormat as F,
)
from models import (
    ResolveState as R,
)
from models.enums import AUDIO_FORMATS, VIDEO_FORMATS


def opts(fmt=F.MP3, **kw) -> DownloadOptions:
    return DownloadOptions(format=fmt, **kw)


def item(status=S.QUEUED, **kw) -> DownloadItem:
    base = dict(item_id="i", job_id="j", url="u")
    return DownloadItem(**{**base, **kw, "status": status})


# ───────────────────────── enums ─────────────────────────


class TestEnums:
    def test_format_partition_is_complete_and_disjoint(self):
        assert AUDIO_FORMATS.isdisjoint(VIDEO_FORMATS)
        assert AUDIO_FORMATS | VIDEO_FORMATS == set(F)

    @pytest.mark.parametrize("fmt", [F.MP3, F.M4A, F.OPUS, F.FLAC, F.WAV])
    def test_audio(self, fmt):
        assert fmt.is_audio and not fmt.is_video

    @pytest.mark.parametrize("fmt", [F.MP4, F.MKV, F.WEBM])
    def test_video(self, fmt):
        assert fmt.is_video and not fmt.is_audio

    def test_only_mp3_and_m4a_take_bitrates(self):
        assert {f for f in F if f.lossy_bitrate} == {F.MP3, F.M4A}

    def test_status_flags(self):
        assert {s for s in S if s.is_terminal} == {S.COMPLETED, S.FAILED, S.CANCELLED, S.PARTIAL}
        assert {s for s in S if s.is_running} == {S.DOWNLOADING, S.PROCESSING}

    def test_string_values_are_api_contract(self):
        assert F.MP3.value == "mp3" and S.QUEUED.value == "queued"


# ───────────────────────── DownloadOptions ─────────────────────────


class TestDownloadOptions:
    @pytest.mark.parametrize("q", ["64", "96", "128", "160", "192", "256", "320", "best"])
    def test_mp3_accepts_bitrates(self, q):
        assert opts(F.MP3, quality=q).quality == q

    @pytest.mark.parametrize("q", ["100", "500", "1080", "worst", "abc"])
    def test_mp3_rejects_other(self, q):
        with pytest.raises(ValidationError, match="Audio quality"):
            opts(F.MP3, quality=q)

    @pytest.mark.parametrize("q", ["144", "240", "360", "480", "720", "1080", "1440", "2160", "best"])
    def test_mp4_accepts_heights(self, q):
        assert opts(F.MP4, quality=q).quality == q

    @pytest.mark.parametrize("q", ["192", "999", "8k", "720x480"])
    def test_mp4_rejects_other(self, q):
        with pytest.raises(ValidationError, match="Video quality"):
            opts(F.MP4, quality=q)

    @pytest.mark.parametrize("raw,expected", [("1080p", "1080"), ("320k", "320"), ("320kbps", "320"),
                                              ("BEST", "best"), (720, "720"), (None, "best"), ("", "best"), (" 480P ", "480")])
    def test_quality_normalisation(self, raw, expected):
        fmt = F.MP4 if expected in ("1080", "720", "480") else F.MP3
        assert DownloadOptions(format=fmt, quality=raw).quality == expected

    @pytest.mark.parametrize("fmt", [F.OPUS, F.FLAC, F.WAV])
    def test_lossless_and_opus_ignore_quality(self, fmt):
        assert opts(fmt, quality="192").quality == "best"
        assert opts(fmt, quality="1080").quality == "best"  # even nonsense is tolerated

    def test_m4a_takes_bitrate(self):
        assert opts(F.M4A, quality="256").quality == "256"

    def test_subtitles_only_for_video(self):
        assert opts(F.MP3, subtitles=True).subtitles is False
        assert opts(F.MP4, subtitles=True).subtitles is True
        assert opts(F.WEBM, subtitles=True).subtitles is True

    def test_subtitle_langs_cleaned_and_defaulted(self):
        assert opts(F.MP4, subtitle_langs=[" en ", "", "hi"]).subtitle_langs == ["en", "hi"]
        assert opts(F.MP4, subtitle_langs=["", " "]).subtitle_langs == ["en"]

    def test_speed_limit_must_be_positive(self):
        with pytest.raises(ValidationError):
            opts(speed_limit=0)
        assert opts(speed_limit=1).speed_limit == 1

    def test_invalid_format_rejected(self):
        with pytest.raises(ValidationError):
            DownloadOptions(format="exe")  # type: ignore[arg-type]

    def test_json_roundtrip(self):
        o = opts(F.MP4, quality="720", subtitles=True)
        assert DownloadOptions.model_validate(o.model_dump(mode="json")) == o


# ───────────────────────── DownloadRequest ─────────────────────────


class TestDownloadRequest:
    def test_trims_and_dedupes_preserving_order(self):
        r = DownloadRequest(urls=[" a ", "b", "a", "", "  ", "c", "b"])
        assert r.urls == ["a", "b", "c"]

    def test_multiline_string_is_split(self):
        assert DownloadRequest(urls="a\nb\n\nc").urls == ["a", "b", "c"]

    @pytest.mark.parametrize("urls", [[], [""], ["  ", "\n"], ""])
    def test_requires_at_least_one_url(self, urls):
        with pytest.raises(ValidationError):
            DownloadRequest(urls=urls)

    def test_max_fifty_urls(self):
        with pytest.raises(ValidationError):
            DownloadRequest(urls=[f"u{i}" for i in range(51)])
        assert len(DownloadRequest(urls=[f"u{i}" for i in range(50)]).urls) == 50

    def test_defaults(self):
        r = DownloadRequest(urls=["a"])
        assert r.format is None and r.quality is None and r.playlist_mode.value == "auto"
        assert r.subtitles is False and r.folder is None

    def test_unknown_playlist_mode_rejected(self):
        with pytest.raises(ValidationError):
            DownloadRequest(urls=["a"], playlist_mode="maybe")  # type: ignore[arg-type]


# ───────────────────────── status derivation ─────────────────────────


class TestDeriveJobStatus:
    @pytest.mark.parametrize(
        "statuses,expected",
        [
            ([S.QUEUED], S.QUEUED),
            ([S.DOWNLOADING], S.DOWNLOADING),
            ([S.PROCESSING], S.PROCESSING),
            ([S.PAUSED], S.PAUSED),
            ([S.COMPLETED], S.COMPLETED),
            ([S.FAILED], S.FAILED),
            ([S.CANCELLED], S.CANCELLED),
            ([S.COMPLETED, S.COMPLETED], S.COMPLETED),
            ([S.COMPLETED, S.FAILED], S.PARTIAL),
            ([S.COMPLETED, S.CANCELLED], S.PARTIAL),
            ([S.FAILED, S.CANCELLED], S.FAILED),
            ([S.CANCELLED, S.CANCELLED], S.CANCELLED),
            ([S.DOWNLOADING, S.QUEUED, S.PAUSED], S.DOWNLOADING),
            ([S.PROCESSING, S.QUEUED], S.PROCESSING),
            ([S.DOWNLOADING, S.PROCESSING], S.DOWNLOADING),
            ([S.COMPLETED, S.QUEUED], S.DOWNLOADING),  # mid-playlist gap still "in progress"
            ([S.QUEUED, S.QUEUED], S.QUEUED),
            ([S.QUEUED, S.PAUSED], S.QUEUED),
            ([S.COMPLETED, S.PAUSED], S.PAUSED),
            ([S.PAUSED, S.PAUSED], S.PAUSED),
            ([S.COMPLETED, S.FAILED, S.PAUSED], S.PAUSED),
        ],
    )
    def test_table(self, statuses, expected):
        items = [item(s) for s in statuses]
        assert derive_job_status(R.READY, None, items) is expected

    @pytest.mark.parametrize("state", [R.PENDING, R.RESOLVING])
    def test_resolving_beats_everything(self, state):
        assert derive_job_status(state, None, [item(S.COMPLETED)]) is S.RESOLVING

    def test_no_items(self):
        assert derive_job_status(R.READY, None, []) is S.QUEUED
        assert derive_job_status(R.FAILED, "boom", []) is S.FAILED
        assert derive_job_status(R.READY, "boom", []) is S.FAILED


# ───────────────────────── JobView ─────────────────────────


def job(**kw) -> DownloadJob:
    return DownloadJob(job_id="j", url="u", options=opts(), **kw)


class TestJobView:
    def test_aggregates(self):
        items = [
            item(S.COMPLETED, percent=100, downloaded_bytes=100, total_bytes=100),
            item(S.DOWNLOADING, percent=50, speed=1000.0, eta=10, downloaded_bytes=50, total_bytes=100),
            item(S.DOWNLOADING, percent=0, speed=500.0, eta=30, downloaded_bytes=0, total_bytes=200),
            item(S.QUEUED),
            item(S.FAILED, percent=20),
            item(S.CANCELLED, percent=99),  # excluded from percent
        ]
        v = JobView.build(job(total_items=6), items)
        assert v.counts == {"completed": 1, "downloading": 2, "queued": 1, "failed": 1, "cancelled": 1}
        assert v.speed == 1500.0 and v.eta == 30
        assert v.downloaded_bytes == 150
        assert v.total_bytes == 400
        assert v.percent == pytest.approx((100 + 50 + 0 + 0 + 20) / 5, abs=0.1)
        assert v.status is S.DOWNLOADING

    def test_completed_items_count_as_100_even_if_percent_stale(self):
        v = JobView.build(job(), [item(S.COMPLETED, percent=0)])
        assert v.percent == 100.0

    def test_speed_ignores_non_running_items(self):
        v = JobView.build(job(), [item(S.PAUSED, speed=999.0), item(S.DOWNLOADING, speed=1.0)])
        assert v.speed == 1.0

    def test_empty_job(self):
        v = JobView.build(job(), [])
        assert v.percent == 0 and v.speed == 0 and v.eta is None and v.total_bytes is None

    def test_title_falls_back_to_url(self):
        assert JobView.build(job(), [item()]).title == "u"
        assert JobView.build(job(title="Real"), [item()]).title == "Real"

    def test_include_items_flag(self):
        its = [item()]
        assert JobView.build(job(), its).items is None
        assert JobView.build(job(), its, include_items=True).items == its

    def test_total_items_uses_larger_of_declared_and_actual(self):
        assert JobView.build(job(total_items=10), [item()]).total_items == 10
        assert JobView.build(job(total_items=0), [item(), item()]).total_items == 2

    def test_updated_at_is_latest_of_job_and_items(self):
        v = JobView.build(job(updated_at=5), [item(updated_at=9), item(updated_at=7)])
        assert v.updated_at == 9

    def test_resolving_playlist_view(self):
        v = JobView.build(job(kind=JobKind.PLAYLIST, resolve_state=R.RESOLVING), [])
        assert v.status is S.RESOLVING

    def test_serialises_to_json(self):
        v = JobView.build(job(), [item()], include_items=True)
        assert v.model_dump(mode="json")["items"][0]["status"] == "queued"
