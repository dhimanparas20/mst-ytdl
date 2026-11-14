"""Shared fixtures. Env is set BEFORE any app import so settings/Huey pick it up."""

from __future__ import annotations

import os

os.environ.setdefault("TASK_IMMEDIATE", "true")  # Huey: inline + in-memory
os.environ.setdefault("LOG_LEVEL", "WARNING")

import fakeredis  # noqa: E402
import pytest  # noqa: E402

from configs import get_settings, set_redis_override  # noqa: E402
from services import dispatch as dispatch_mod  # noqa: E402
from services import download_runner  # noqa: E402
from tests.fakes import FakeDispatcher, FakeYDL  # noqa: E402


@pytest.fixture(autouse=True)
def fake_redis():
    """Every test gets a clean in-memory Redis."""
    client = fakeredis.FakeRedis(decode_responses=True)
    set_redis_override(client)
    yield client
    client.flushall()
    set_redis_override(None)


@pytest.fixture
def downloads_dir(tmp_path, monkeypatch):
    """Point the downloads folder at a temp dir (resolved, like the real validator does)."""
    root = (tmp_path / "downloads").resolve()
    root.mkdir()
    monkeypatch.setattr(get_settings().download, "downloads_dir", root)
    return root


@pytest.fixture
def dispatcher():
    """Replace the queue with a recorder; restore the previous dispatcher after."""
    previous = dispatch_mod._dispatcher
    fake = FakeDispatcher()
    dispatch_mod.set_dispatcher(fake)
    yield fake
    dispatch_mod.set_dispatcher(previous)


@pytest.fixture
def fake_ydl(monkeypatch):
    """Swap yt-dlp for the scripted FakeYDL inside the runner (fast polls, no sleeps)."""
    FakeYDL.reset()
    monkeypatch.setattr(download_runner, "ydl_factory", FakeYDL)
    monkeypatch.setattr(download_runner, "slot_poll", 0.02)
    monkeypatch.setattr(download_runner, "heartbeat_interval", 0.2)
    yield FakeYDL
    FakeYDL.reset()


@pytest.fixture
def task_settings(monkeypatch):
    """Mutable handle on TaskSettings for per-test tweaks."""
    return get_settings().task
