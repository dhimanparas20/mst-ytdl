"""services/ytdl_opts.py — option mapping (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from configs import get_settings
from models import DownloadOptions
from models import MediaFormat as F
from services.ytdl_opts import (
    base_extract_opts,
    build_postprocessors,
    build_ydl_opts,
    format_selector,
    format_sort,
    friendly_error,
)
from tests.fakes import make_download_error


def o(fmt, **kw) -> DownloadOptions:
    return DownloadOptions(format=fmt, **kw)


def keys(pps):
    return [p["key"] for p in pps]


class TestFormatSelector:
    @pytest.mark.parametrize("fmt", [F.MP3, F.OPUS, F.FLAC, F.WAV])
    def test_audio_takes_best_audio(self, fmt):
        assert format_selector(o(fmt)) == "bestaudio/best"

    def test_m4a_prefers_native_m4a(self):
        assert format_selector(o(F.M4A)).startswith("bestaudio[ext=m4a]")

    @pytest.mark.parametrize("fmt", [F.MP4, F.MKV])
    def test_video_merges_streams_with_progressive_fallback(self, fmt):
        assert format_selector(o(fmt, quality="720")) == "bv*+ba/b"

    def test_webm_prefers_webm_streams(self):
        sel = format_selector(o(F.WEBM))
        assert "ext=webm" in sel and sel.endswith("/b")


class TestFormatSort:
    def test_audio_has_no_sort(self):
        assert format_sort(o(F.MP3)) is None

    @pytest.mark.parametrize("q,expect", [
        ("144", ["res:144", "vcodec:h264", "acodec:m4a"]),
        ("720", ["res:720", "vcodec:h264", "acodec:m4a"]),
        ("1080", ["res:1080", "vcodec:h264", "acodec:m4a"]),
        ("best", ["vcodec:h264", "acodec:m4a"]),
    ])
    def test_mp4_prefers_h264_up_to_1080(self, q, expect):
        assert format_sort(o(F.MP4, quality=q)) == expect

    @pytest.mark.parametrize("q", ["1440", "2160"])
    def test_mp4_above_1080_drops_h264_preference(self, q):
        s = format_sort(o(F.MP4, quality=q))
        assert s == [f"res:{q}", "ext:mp4:m4a"] and "vcodec:h264" not in s

    def test_mkv_only_caps_resolution(self):
        assert format_sort(o(F.MKV, quality="480")) == ["res:480"]
        assert format_sort(o(F.MKV, quality="best")) is None

    def test_webm_caps_resolution(self):
        assert format_sort(o(F.WEBM, quality="360")) == ["res:360"]


class TestPostprocessors:
    def test_mp3_chain_order_and_bitrate(self):
        pps = build_postprocessors(o(F.MP3, quality="256"))
        assert keys(pps) == ["FFmpegExtractAudio", "FFmpegMetadata", "EmbedThumbnail"]
        assert pps[0]["preferredcodec"] == "mp3" and pps[0]["preferredquality"] == "256"

    def test_mp3_best_means_320(self):
        assert build_postprocessors(o(F.MP3, quality="best"))[0]["preferredquality"] == "320"

    def test_m4a_bitrate_only_when_chosen(self):
        assert "preferredquality" not in build_postprocessors(o(F.M4A, quality="best"))[0]
        assert build_postprocessors(o(F.M4A, quality="128"))[0]["preferredquality"] == "128"

    @pytest.mark.parametrize("fmt", [F.OPUS, F.FLAC, F.WAV])
    def test_no_quality_for_lossless_or_opus(self, fmt):
        assert "preferredquality" not in build_postprocessors(o(fmt))[0]

    @pytest.mark.parametrize("fmt,expect", [
        (F.MP3, True), (F.M4A, True), (F.OPUS, True), (F.FLAC, True), (F.MP4, True), (F.MKV, True),
        (F.WAV, False), (F.WEBM, False),  # yt-dlp cannot embed art here → skip instead of failing
    ])
    def test_thumbnail_only_where_supported(self, fmt, expect):
        assert ("EmbedThumbnail" in keys(build_postprocessors(o(fmt)))) is expect

    def test_toggles_off(self):
        pps = build_postprocessors(o(F.MP3, embed_metadata=False, embed_thumbnail=False))
        assert keys(pps) == ["FFmpegExtractAudio"]

    def test_video_has_no_extract_audio(self):
        assert "FFmpegExtractAudio" not in keys(build_postprocessors(o(F.MP4)))

    def test_subtitles_embedded_before_metadata(self):
        k = keys(build_postprocessors(o(F.MP4, subtitles=True)))
        assert k.index("FFmpegEmbedSubtitle") < k.index("FFmpegMetadata")
        assert "FFmpegSubtitlesConvertor" in k

    def test_no_subtitle_pp_when_off(self):
        assert "FFmpegEmbedSubtitle" not in keys(build_postprocessors(o(F.MP4)))

    @pytest.mark.parametrize("fmt", [F.MP3, F.MP4])
    def test_sponsorblock_off_by_default(self, fmt):
        k = keys(build_postprocessors(o(fmt)))
        assert "SponsorBlock" not in k and "ModifyChapters" not in k

    @pytest.mark.parametrize("fmt", [F.MP3, F.MP4])
    def test_sponsorblock_on_adds_lookup_and_cut(self, fmt):
        pps = build_postprocessors(o(fmt, sponsorblock=True))
        k = keys(pps)
        sb = pps[k.index("SponsorBlock")]
        cut = pps[k.index("ModifyChapters")]
        assert sb["categories"] == ["sponsor", "selfpromo", "interaction"]
        assert sb["when"] == "after_filter"
        assert cut["remove_sponsor_segments"] == ["sponsor", "selfpromo", "interaction"]
        # lookup first, cut before the tags/chapters are written, existing steps untouched
        assert k[0] == "SponsorBlock"
        assert k.index("ModifyChapters") < k.index("FFmpegMetadata")
        assert "FFmpegMetadata" in k and "EmbedThumbnail" in k

    def test_sponsorblock_cuts_after_audio_extraction_and_subtitles(self):
        k = keys(build_postprocessors(o(F.MP3, sponsorblock=True)))
        assert k.index("FFmpegExtractAudio") < k.index("ModifyChapters")
        k = keys(build_postprocessors(o(F.MP4, sponsorblock=True, subtitles=True)))
        assert k.index("FFmpegEmbedSubtitle") < k.index("ModifyChapters")

    def test_sponsorblock_without_metadata_still_cuts(self):
        k = keys(build_postprocessors(o(F.MP4, sponsorblock=True, embed_metadata=False, embed_thumbnail=False)))
        assert k == ["SponsorBlock", "ModifyChapters"]


class TestBuildYdlOpts:
    def build(self, opt, **kw):
        return build_ydl_opts(opt, outdir=Path("/tmp/out"), **kw)

    def test_core_flags(self):
        d = self.build(o(F.MP3))
        assert d["noplaylist"] is True and d["continuedl"] is True  # resume depends on this
        assert d["paths"] == {"home": str(Path("/tmp/out"))}
        assert d["windowsfilenames"] is True and d["overwrites"] is None
        assert d["quiet"] is True and d["noprogress"] is True

    def test_outtmpl_plain_and_with_prefix_and_id(self, monkeypatch):
        assert self.build(o(F.MP3))["outtmpl"]["default"] == "%(title).150B.%(ext)s"
        assert self.build(o(F.MP3), filename_prefix="03 - ")["outtmpl"]["default"].startswith("03 - %(title)")
        monkeypatch.setattr(get_settings().download, "include_id_in_name", True)
        assert "[%(id)s]" in self.build(o(F.MP3))["outtmpl"]["default"]

    def test_video_sets_merge_container_audio_does_not(self):
        assert self.build(o(F.MP4))["merge_output_format"] == "mp4"
        assert self.build(o(F.MKV))["merge_output_format"] == "mkv"
        assert "merge_output_format" not in self.build(o(F.MP3))

    def test_writethumbnail_matches_embed_capability(self):
        assert self.build(o(F.MP3))["writethumbnail"] is True
        assert self.build(o(F.WAV))["writethumbnail"] is False
        assert self.build(o(F.MP3, embed_thumbnail=False))["writethumbnail"] is False

    def test_sponsorblock_reaches_ydl_postprocessors(self):
        assert "ModifyChapters" in keys(self.build(o(F.MP4, sponsorblock=True))["postprocessors"])
        assert "ModifyChapters" not in keys(self.build(o(F.MP4))["postprocessors"])

    def test_speed_limit_priority_job_then_runtime_then_env(self, monkeypatch):
        monkeypatch.setattr(get_settings().download, "speed_limit", 300)
        assert self.build(o(F.MP3, speed_limit=100), runtime_speed_limit=200)["ratelimit"] == 100
        assert self.build(o(F.MP3), runtime_speed_limit=200)["ratelimit"] == 200
        assert self.build(o(F.MP3))["ratelimit"] == 300
        monkeypatch.setattr(get_settings().download, "speed_limit", None)
        assert "ratelimit" not in self.build(o(F.MP3))

    def test_subtitle_options(self):
        d = self.build(o(F.MP4, subtitles=True, subtitle_langs=["en", "hi"]))
        assert d["writesubtitles"] and d["writeautomaticsub"] and d["subtitleslangs"] == ["en", "hi"]
        assert "writesubtitles" not in self.build(o(F.MP3, subtitles=True))

    def test_hooks_are_wired(self):
        a, b, c = (lambda d: 1), (lambda d: 2), (lambda p: 3)
        d = self.build(o(F.MP3), progress_hook=a, postprocessor_hook=b, post_hook=c)
        assert d["progress_hooks"] == [a] and d["postprocessor_hooks"] == [b] and d["post_hooks"] == [c]
        assert "progress_hooks" not in self.build(o(F.MP3))

    def test_ffmpeg_location_from_clients(self):
        loc = self.build(o(F.MP3)).get("ffmpeg_location")
        assert loc and Path(loc).is_file()

    def test_cookies_and_proxy(self, monkeypatch, tmp_path):
        dl = get_settings().download
        (tmp_path / "c.txt").write_text("# Netscape HTTP Cookie File")  # only an existing file is passed on
        monkeypatch.setattr(dl, "cookies_file", tmp_path / "c.txt")
        monkeypatch.setattr(dl, "cookies_from_browser", "firefox")
        monkeypatch.setattr(dl, "proxy", "http://proxy:3128")
        d = self.build(o(F.MP3))
        assert d["cookiefile"].endswith("c.txt") and d["cookiesfrombrowser"] == ("firefox",)
        assert d["proxy"] == "http://proxy:3128"

    def test_concurrent_fragments_at_least_one(self, monkeypatch):
        monkeypatch.setattr(get_settings().download, "concurrent_fragments", 0)
        assert self.build(o(F.MP3))["concurrent_fragment_downloads"] == 1

    def test_no_ansi_colour_codes(self):
        assert self.build(o(F.MP3))["color"] == {"stdout": "never", "stderr": "never"}

    def test_overwrite_flag(self):
        assert self.build(o(F.MP3))["overwrites"] is None
        assert self.build(o(F.MP3, overwrite=True))["overwrites"] is True

    def test_options_are_accepted_by_real_yt_dlp(self, tmp_path):
        """The dict must construct a real YoutubeDL (catches typos in param names)."""
        from yt_dlp import YoutubeDL

        for fmt in (F.MP3, F.M4A, F.OPUS, F.FLAC, F.WAV, F.MP4, F.MKV, F.WEBM):
            params = build_ydl_opts(o(fmt, subtitles=fmt.is_video), outdir=tmp_path)
            with YoutubeDL(params) as ydl:
                assert ydl.params["format"] == params["format"]
                # post-processors must all resolve to real classes
                assert len(ydl._pps["post_process"]) + len(ydl._pps["before_dl"]) >= len(params["postprocessors"]) - 1


class TestExtractOpts:
    def test_single_vs_flat(self):
        single = base_extract_opts()
        assert single["noplaylist"] is True and "extract_flat" not in single
        flat = base_extract_opts(flat=True, max_items=50)
        assert flat["extract_flat"] == "in_playlist" and flat["playlistend"] == 50 and flat["noplaylist"] is False

    def test_never_downloads(self):
        assert base_extract_opts()["skip_download"] is True


class TestFriendlyError:
    def test_strips_prefixes(self):
        msg, _ = friendly_error(make_download_error("[youtube] dQw4w9WgXcQ: Video unavailable"))
        assert msg == "Video unavailable" and "ERROR" not in msg

    @pytest.mark.parametrize("text", [
        "Private video. Sign in if you've been granted access",
        "Video unavailable. This video is not available",
        "Sign in to confirm you're not a bot",
        "This video is only available for members-only",
        "Join this channel to get access to members-only content",
        "The uploader has not made this video available in your country",
        "Requested format is not available",
        "ffmpeg is not installed",
    ])
    def test_permanent_errors_are_not_retryable(self, text):
        assert friendly_error(make_download_error(text))[1] is False

    def test_expected_extractor_error_is_not_retryable(self):
        assert friendly_error(make_download_error("some novel permanent reason", expected=True))[1] is False

    @pytest.mark.parametrize("text", ["HTTP Error 503: Service Unavailable", "Connection reset by peer",
                                      "Read timed out", "unable to download video data"])
    def test_transient_download_errors_are_retryable(self, text):
        assert friendly_error(make_download_error(text))[1] is True

    def test_non_download_errors_are_never_retryable(self):
        msg, retry = friendly_error(RuntimeError("bug in our code"))
        assert msg == "bug in our code" and retry is False

    def test_message_is_truncated_and_never_empty(self):
        assert len(friendly_error(make_download_error("x" * 2000))[0]) <= 500
        assert friendly_error(ValueError(""))[0] == "ValueError"

    def test_ansi_codes_removed(self):
        msg, _ = friendly_error(make_download_error("\x1b[0;31mERROR:\x1b[0m boom"))
        assert "\x1b" not in msg
