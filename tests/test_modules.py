"""modules/: generics, ytdlp_util, redis_util, logger."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from modules import generics
from modules.logger import RedactionFilter, get_logger
from modules.redis_util import decode_hash, encode_hash, validate_redis_connection
from modules.ytdlp_util import (
    UrlError,
    find_ffmpeg,
    find_js_runtime,
    normalize_url,
    parse_selection,
    parse_url,
    remove_partial_files,
    sanitize_filename,
    sanitize_relpath,
    validate_host,
    video_url,
)

# ───────────────────────── generics ─────────────────────────


class TestGenerics:
    @pytest.mark.parametrize(
        "text,expected",
        [("2048", 2048), ("500K", 500 * 1024), ("2M", 2 * 1024**2), ("1.5GB", int(1.5 * 1024**3)),
         (" 3 mb ", 3 * 1024**2), (7, 7), (7.9, 7)],
    )
    def test_parse_size(self, text, expected):
        assert generics.parse_size(text) == expected

    @pytest.mark.parametrize("bad", ["abc", "12X", "", "-5M", "M2"])
    def test_parse_size_rejects(self, bad):
        with pytest.raises(ValueError):
            generics.parse_size(bad)

    @pytest.mark.parametrize(
        "n,expected",
        [(None, "-"), (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KiB"), (1536, "1.5 KiB"),
         (5 * 1024**2, "5.0 MiB"), (3 * 1024**3, "3.0 GiB"), (2 * 1024**4, "2.0 TiB")],
    )
    def test_human_bytes(self, n, expected):
        assert generics.human_bytes(n) == expected

    @pytest.mark.parametrize(
        "s,expected",
        [(None, "-"), (0, "0:00"), (5, "0:05"), (65, "1:05"), (3600, "1:00:00"), (3725, "1:02:05")],
    )
    def test_human_duration(self, s, expected):
        assert generics.human_duration(s) == expected

    def test_new_id_unique_and_url_safe(self):
        ids = {generics.new_id() for _ in range(500)}
        assert len(ids) == 500
        assert all(re.fullmatch(r"[0-9a-f]{12}", i) for i in ids)

    def test_today_str_default_and_custom_format(self):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", generics.today_str("Asia/Kolkata"))
        assert re.fullmatch(r"\d{2}-\d{2}-\d{4}", generics.today_str("UTC", "%d-%m-%Y"))

    def test_timezone_changes_the_date_boundary(self):
        # Kiritimati (UTC+14) is always 14h ahead of Baker Island (UTC-12): 26h apart
        a = generics.now_tz("Pacific/Kiritimati").replace(tzinfo=None)
        b = generics.now_tz("Etc/GMT+12").replace(tzinfo=None)
        assert 25 * 3600 < (a - b).total_seconds() < 27 * 3600

    def test_unknown_timezone_falls_back_to_utc(self):
        assert str(generics.get_tz("Not/AZone")) == "UTC"

    def test_now_iso_and_timestamp(self):
        assert "T" in generics.now_iso("UTC")
        assert generics.get_timestamp() > 1_700_000_000


# ───────────────────────── URL parsing ─────────────────────────

VID = "dQw4w9WgXcQ"
PL = "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"


class TestParseUrl:
    @pytest.mark.parametrize(
        "url",
        [
            f"https://www.youtube.com/watch?v={VID}",
            f"http://youtube.com/watch?v={VID}&t=30s",
            f"youtube.com/watch?v={VID}",  # no scheme
            f"https://m.youtube.com/watch?v={VID}",
            f"https://youtu.be/{VID}",
            f"https://youtu.be/{VID}?si=abc",
            f"https://www.youtube.com/shorts/{VID}",
            f"https://www.youtube.com/live/{VID}",
            f"https://www.youtube.com/embed/{VID}",
            f"https://music.youtube.com/watch?v={VID}",
        ],
    )
    def test_videos(self, url):
        p = parse_url(url)
        assert p.path_kind == "video" and p.video_id == VID and not p.ambiguous

    def test_music_flag(self):
        assert parse_url(f"https://music.youtube.com/watch?v={VID}").is_music
        assert not parse_url(f"https://www.youtube.com/watch?v={VID}").is_music

    @pytest.mark.parametrize(
        "url,host",
        [(f"https://www.youtube.com/playlist?list={PL}", "youtube.com"),
         ("https://music.youtube.com/playlist?list=OLAK5uy_abc", "music.youtube.com")],
    )
    def test_playlists(self, url, host):
        p = parse_url(url)
        assert p.path_kind == "playlist" and p.list_id and p.host == host

    def test_watch_with_list_is_ambiguous(self):
        p = parse_url(f"https://www.youtube.com/watch?v={VID}&list={PL}&index=3")
        assert p.ambiguous and p.video_id == VID and p.list_id == PL

    def test_list_only_on_watch_path_becomes_playlist(self):
        assert parse_url(f"https://www.youtube.com/watch?list={PL}").path_kind == "playlist"

    @pytest.mark.parametrize(
        "url,kind",
        [("https://www.youtube.com/@SomeChannel", "channel"),
         ("https://www.youtube.com/channel/UC1234567890abcdefghij", "channel"),
         ("https://www.youtube.com/c/SomeName/videos", "channel"),
         ("https://www.youtube.com/user/legacy", "channel"),
         ("https://music.youtube.com/browse/MPREb_abcdefgh", "browse")],
    )
    def test_channels_and_browse(self, url, kind):
        assert parse_url(url).path_kind == kind

    @pytest.mark.parametrize(
        "url,msg",
        [("", "Empty"),
         ("   ", "Empty"),
         ("ftp://youtube.com/watch?v=" + VID, "http"),
         ("https://vimeo.com/12345", "Unsupported host"),
         ("https://evil.example.com/watch?v=" + VID, "Unsupported host"),
         ("https://notyoutube.com/watch?v=" + VID, "Unsupported host"),
         ("https://www.youtube.com/watch?v=short", "valid video id"),
         ("https://www.youtube.com/watch", "No video id"),
         ("https://www.youtube.com/playlist", "No playlist id"),
         ("https://www.youtube.com/", "Could not find")],
    )
    def test_rejections(self, url, msg):
        with pytest.raises(UrlError, match=msg):
            parse_url(url)

    def test_lookalike_subdomain_rejected(self):
        with pytest.raises(UrlError):
            parse_url(f"https://youtube.com.evil.io/watch?v={VID}")


class TestHostAndNormalize:
    def test_validate_host_allows_configured_and_normalises_www(self):
        p = parse_url(f"https://www.youtube.com/watch?v={VID}")
        validate_host(p, ["youtube.com"])  # www. stripped on both sides
        validate_host(p, ["www.youtube.com"])

    def test_validate_host_rejects(self):
        p = parse_url(f"https://music.youtube.com/watch?v={VID}")
        with pytest.raises(UrlError, match="not allowed"):
            validate_host(p, ["youtube.com", "youtu.be"])

    def test_normalize_single_drops_noise(self):
        p = parse_url(f"https://www.youtube.com/watch?v={VID}&list={PL}&index=2&t=99")
        assert normalize_url(p, playlist=False) == f"https://www.youtube.com/watch?v={VID}"

    def test_normalize_keeps_music_host(self):
        p = parse_url(f"https://music.youtube.com/watch?v={VID}&si=x")
        assert normalize_url(p, playlist=False) == f"https://music.youtube.com/watch?v={VID}"

    def test_normalize_playlist(self):
        p = parse_url(f"https://www.youtube.com/watch?v={VID}&list={PL}")
        assert normalize_url(p, playlist=True) == f"https://www.youtube.com/playlist?list={PL}"

    def test_normalize_mix_keeps_video_context(self):
        # YouTube Mix / YT Music radio (list=RD…) is "unviewable" as a bare playlist URL
        p = parse_url(f"https://music.youtube.com/watch?v={VID}&list=RDAMVM{VID}")
        assert normalize_url(p, playlist=True) == f"https://music.youtube.com/watch?v={VID}&list=RDAMVM{VID}"
        # …but a regular playlist id still collapses to the canonical playlist URL
        q = parse_url(f"https://www.youtube.com/watch?v={VID}&list={PL}")
        assert normalize_url(q, playlist=True) == f"https://www.youtube.com/playlist?list={PL}"

    def test_normalize_channel_appends_videos_tab(self):
        p = parse_url("https://www.youtube.com/@SomeChannel")
        assert normalize_url(p, playlist=True) == "https://www.youtube.com/@SomeChannel/videos"
        p2 = parse_url("https://www.youtube.com/@SomeChannel/shorts")
        assert normalize_url(p2, playlist=True) == "https://www.youtube.com/@SomeChannel/shorts"

    def test_normalize_browse_passthrough(self):
        p = parse_url("https://music.youtube.com/browse/MPREb_abcdefgh")
        assert normalize_url(p, playlist=True) == "https://music.youtube.com/browse/MPREb_abcdefgh"

    def test_video_url(self):
        assert video_url(VID) == f"https://www.youtube.com/watch?v={VID}"
        assert video_url(VID, music=True) == f"https://music.youtube.com/watch?v={VID}"


# ───────────────────────── filenames ─────────────────────────


class TestSanitize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("My Playlist", "My Playlist"),
            ('AC/DC: "Back" in <Black>?', "ACDC Back in Black"),
            ("  spaced   out  ", "spaced out"),
            ("trailing dots...", "trailing dots"),
            ("a\\b|c*d", "abcd"),
            ("tab\tand\nnewline", "tab and newline"),
            ("", "untitled"),
            ("???", "untitled"),
            ("...", "untitled"),
            ("日本語のプレイリスト", "日本語のプレイリスト"),
            ("emoji 🎵 mix", "emoji 🎵 mix"),
        ],
    )
    def test_cases(self, raw, expected):
        assert sanitize_filename(raw) == expected

    @pytest.mark.parametrize("name", ["CON", "con", "NUL", "COM1", "LPT9", "aux.txt"])
    def test_reserved_windows_names_are_defused(self, name):
        out = sanitize_filename(name)
        assert out != name and out.startswith("_")

    def test_truncates_without_trailing_space_or_dot(self):
        out = sanitize_filename("a" * 140 + " .x" * 10, max_len=150)
        assert len(out) <= 150 and not out.endswith((" ", "."))

    def test_path_traversal_text_is_flattened(self):
        out = sanitize_filename("../../etc/passwd")
        assert "/" not in out and "\\" not in out and not out.startswith(".")

    def test_custom_fallback(self):
        assert sanitize_filename("", fallback="playlist") == "playlist"

    def test_none_is_safe(self):
        assert sanitize_filename(None) == "untitled"  # type: ignore[arg-type]


class TestSanitizeRelpath:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Music/Rock", "Music/Rock"),
            ("Music\\Rock", "Music/Rock"),
            ("  Music  /  Rock  ", "Music/Rock"),
            ("../../etc", "etc"),
            ("a/../b/./c", "a/b/c"),
            ("/abs/path", "abs/path"),
            ("C:\\Windows\\System32", "C/Windows/System32"),
            ("a//b///c", "a/b/c"),
            ('we"ird/na:me', "weird/name"),
            ("", "untitled"),
            ("..", "untitled"),
            ("///", "untitled"),
            (None, "untitled"),
            ("a/b/c/d/e/f", "a/b/c/d"),
            ("CON/ok", "_CON/ok"),
        ],
    )
    def test_cases(self, raw, expected):
        assert sanitize_relpath(raw) == expected

    def test_custom_depth_length_and_fallback(self):
        assert sanitize_relpath("a/b/c", max_depth=2) == "a/b"
        assert len(sanitize_relpath("x" * 300, max_len=20)) == 20
        assert sanitize_relpath("", fallback="") == ""

    def test_never_yields_traversal_or_absolute_paths(self):
        for evil in ("../x", "..\\x", "/etc/passwd", "a/../../b", "....//x", "%2e%2e/x"):
            out = sanitize_relpath(evil)
            assert not out.startswith(("/", "..")) and ".." not in out.split("/")


class TestParseSelection:
    @pytest.mark.parametrize(
        "spec,total,expected",
        [
            (None, 4, [1, 2, 3, 4]),
            ("", 3, [1, 2, 3]),
            ("   ", 3, [1, 2, 3]),
            ("1,3,5-7", 10, [1, 3, 5, 6, 7]),
            ("8-", 10, [8, 9, 10]),
            ("-3", 10, [1, 2, 3]),
            ("2,2,2", 5, [2]),
            ("5-3,1", 5, None),  # inverted range → error
            ("1, 2 ,3", 5, [1, 2, 3]),
            ("0,1,99", 5, [1]),  # out-of-range silently dropped
            ("3,1,2", 5, [1, 2, 3]),  # sorted
        ],
    )
    def test_cases(self, spec, total, expected):
        if expected is None:
            with pytest.raises(ValueError):
                parse_selection(spec, total)
        else:
            assert parse_selection(spec, total) == expected

    @pytest.mark.parametrize("spec", ["abc", "1,x", "1-2-3", "99", "0"])
    def test_garbage_or_empty_result_raises(self, spec):
        with pytest.raises(ValueError):
            parse_selection(spec, 5)


# ───────────────────────── tool discovery ─────────────────────────


class TestToolDiscovery:
    def test_find_ffmpeg_returns_usable_binary(self):
        exe = find_ffmpeg(None)
        assert exe and Path(exe).is_file()

    def test_find_ffmpeg_prefers_configured_file(self, tmp_path):
        fake = tmp_path / "myffmpeg.exe"
        fake.write_bytes(b"")
        assert find_ffmpeg(str(fake)) == str(fake)

    def test_find_ffmpeg_configured_directory(self, tmp_path):
        exe = tmp_path / ("ffmpeg.exe" if Path(find_ffmpeg() or "x").suffix == ".exe" else "ffmpeg")
        exe.write_bytes(b"")
        assert find_ffmpeg(str(tmp_path)) == str(exe)

    def test_find_ffmpeg_bad_config_falls_back(self):
        assert find_ffmpeg("/definitely/not/here") == find_ffmpeg(None)

    def test_find_js_runtime_shape(self):
        js = find_js_runtime("auto")
        if js is not None:
            (name, cfg), = js.items()
            assert name in {"deno", "node", "bun", "quickjs"} and Path(cfg["path"]).exists()

    def test_find_js_runtime_unknown_preference(self):
        assert find_js_runtime("definitely-not-a-runtime") is None


class TestRemovePartialFiles:
    def test_removes_companions_and_counts(self, tmp_path):
        base = tmp_path / "song.f251.webm"
        for suffix in ("", ".part", ".ytdl"):
            Path(str(base) + suffix).write_bytes(b"x")
        keep = tmp_path / "keep.mp3"
        keep.write_bytes(b"x")
        assert remove_partial_files([str(base)]) == 3
        assert keep.exists() and not Path(str(base) + ".part").exists()

    def test_missing_files_are_fine(self, tmp_path):
        assert remove_partial_files([str(tmp_path / "ghost.mp4")]) == 0


# ───────────────────────── redis_util ─────────────────────────


class TestRedisUtil:
    def test_encode_decode_roundtrip_preserves_types(self):
        data = {"n": None, "f": 1.5, "i": 3, "b": True, "s": "héllo", "l": ["a", 1], "d": {"k": None}}
        assert decode_hash(encode_hash(data)) == data

    def test_decode_tolerates_non_json_and_none(self):
        assert decode_hash({"x": "not json"}) == {"x": "not json"}
        assert decode_hash(None) == {}
        assert decode_hash({}) == {}

    def test_encoded_values_are_strings(self):
        assert all(isinstance(v, str) for v in encode_hash({"a": 1, "b": None}).values())

    @pytest.mark.parametrize("url", ["", "   ", "http://localhost:6379", "redis://", "memcached://x"])
    def test_validate_rejects_bad_urls(self, url):
        with pytest.raises(ValueError):
            validate_redis_connection(url)

    def test_validate_unreachable_raises_connection_error(self):
        with pytest.raises(ConnectionError):
            validate_redis_connection("redis://127.0.0.1:1/0", timeout=0.3)


# ───────────────────────── logger ─────────────────────────


class TestLogger:
    def test_success_level_exists(self):
        log = get_logger("test.success.level", level="DEBUG")
        assert hasattr(log, "success") and logging.getLevelName(25) == "SUCCESS"
        log.success("works")  # must not raise

    def test_logger_is_configured_once(self):
        a = get_logger("test.once")
        b = get_logger("test.once")
        assert a is b and len(a.handlers) == 1

    def test_json_output_is_valid_json(self, capsys):
        import json

        log = get_logger("test.json.out", json_output=True, level="INFO")
        log.info("hello %s", "world")
        err = capsys.readouterr().err.strip()
        parsed = json.loads(err)
        assert parsed["msg"] == "hello world" and parsed["level"] == "INFO"

    @pytest.mark.parametrize(
        "raw,must_not_contain",
        [("password=hunter2", "hunter2"), ("api_key: abc123", "abc123"),
         ("Authorization: Bearer eyJhbGci", "eyJhbGci"), ("token=XYZ", "XYZ"),
         ("Cookie: SID=secretvalue", "secretvalue")],
    )
    def test_redaction(self, raw, must_not_contain):
        rec = logging.LogRecord("t", logging.INFO, "f", 1, raw, None, None)
        RedactionFilter().filter(rec)
        assert must_not_contain not in rec.getMessage()
