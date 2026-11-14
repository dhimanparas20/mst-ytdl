"""configs/: settings resolution + client singletons."""

from __future__ import annotations

from pathlib import Path

import fakeredis
import pytest

from configs import BASE_DIR, clients, get_settings, set_redis_override
from configs.config import DownloadSettings, FastAPISettings, RedisSettings, TaskSettings


class TestDownloadSettings:
    def test_relative_downloads_dir_resolves_against_repo_not_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        s = DownloadSettings(downloads_dir=Path("downloads"))
        assert s.downloads_dir == (BASE_DIR / "downloads").resolve()

    def test_absolute_downloads_dir_kept(self, tmp_path):
        assert DownloadSettings(downloads_dir=tmp_path).downloads_dir == tmp_path.resolve()

    @pytest.mark.parametrize("raw,expected", [("2M", 2 * 1024**2), ("500K", 512000), ("", None), (None, None), (1000, 1000)])
    def test_speed_limit_parsing(self, raw, expected):
        assert DownloadSettings(speed_limit=raw).speed_limit == expected

    def test_speed_limit_garbage_is_a_config_error(self):
        with pytest.raises(ValueError):
            DownloadSettings(speed_limit="fast")

    @pytest.mark.parametrize("field", ["ffmpeg_path", "cookies_file", "cookies_from_browser", "proxy"])
    def test_blank_optionals_become_none(self, field):
        assert getattr(DownloadSettings(**{field: "  "}), field) is None

    def test_env_overrides_and_json_list(self, monkeypatch):
        monkeypatch.setenv("DL_TIMEZONE", "UTC")
        monkeypatch.setenv("DL_PLAYLIST_NUMBERING", "false")
        monkeypatch.setenv("DL_ALLOWED_HOSTS", '["youtube.com"]')
        monkeypatch.setenv("DL_CONCURRENT_FRAGMENTS", "8")
        s = DownloadSettings()
        assert s.timezone == "UTC" and s.playlist_numbering is False
        assert s.allowed_hosts == ["youtube.com"] and s.concurrent_fragments == 8

    def test_defaults_are_sane(self, monkeypatch):
        for k in ("DL_TIMEZONE", "DL_DEFAULT_FORMAT"):
            monkeypatch.delenv(k, raising=False)
        s = DownloadSettings(_env_file=None)
        assert s.default_format == "mp3" and s.timezone == "Asia/Kolkata"
        assert "music.youtube.com" in s.allowed_hosts and "youtu.be" in s.allowed_hosts


class TestOtherSettings:
    def test_api_defaults_bind_loopback(self):
        assert FastAPISettings(_env_file=None).host == "127.0.0.1"

    def test_task_env_prefix(self, monkeypatch):
        monkeypatch.setenv("TASK_WORKERS", "9")
        monkeypatch.setenv("TASK_MAX_PARALLEL", "5")
        t = TaskSettings()
        assert (t.workers, t.max_parallel) == (9, 5)

    def test_redis_settings(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "redis://example:1234/2")
        monkeypatch.setenv("REDIS_KEY_PREFIX", "XX")
        r = RedisSettings()
        assert r.redis_url == "redis://example:1234/2" and r.redis_key_prefix == "XX"

    def test_get_settings_is_cached_singleton(self):
        assert get_settings() is get_settings()
        s = get_settings()
        assert s.download and s.task and s.api and s.redis

    def test_env_file_path_is_absolute(self):
        from configs.config import ENV_FILE

        assert ENV_FILE.is_absolute() and ENV_FILE.parent == BASE_DIR


class TestClients:
    def test_override_swaps_and_restores(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        set_redis_override(fake)
        assert clients.get_redis() is fake
        set_redis_override(None)
        assert clients.get_redis() is not fake  # real (lazy) client; no connection made yet
        set_redis_override(fake)  # leave fixture state consistent

    def test_get_redis_is_cached(self):
        assert clients.get_redis() is clients.get_redis()

    def test_key_prefix_from_settings(self):
        assert clients.get_key_prefix() == get_settings().redis.redis_key_prefix

    def test_ffmpeg_and_js_getters(self):
        exe = clients.get_ffmpeg_path()
        assert exe is None or Path(exe).is_file()
        js = clients.get_js_runtimes()
        assert js is None or isinstance(js, dict)
