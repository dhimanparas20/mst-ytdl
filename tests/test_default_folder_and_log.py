"""Default download folder (runtime setting) and the worker's progress log lines (TASK_LOG_PROGRESS)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from services import metadata_service, runtime_service
from services.downloader import progress_line, progress_log_due


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


# ───────────── default folder ─────────────
def test_default_folder_unset_by_default():
    assert runtime_service.get().default_folder is None


def test_set_clear_and_sanitise():
    assert runtime_service.update(default_folder="Music/Rock").default_folder == "Music/Rock"
    assert runtime_service.update(default_folder=None).default_folder is None
    assert runtime_service.update(default_folder="../../etc").default_folder == "etc"      # no traversal
    assert runtime_service.update(default_folder=".tmp/x").default_folder == "tmp/x"          # leading dots are stripped: never hidden
    assert runtime_service.update(default_folder="  ").default_folder is None
    assert runtime_service.update(max_parallel=2).default_folder is None                    # other updates leave it


def test_folders_use_the_base_folder_but_overrides_win():
    today = metadata_service.folder_for_single()
    runtime_service.update(default_folder="Music")
    assert metadata_service.folder_for_single() == "Music"
    assert metadata_service.folder_for_playlist("Road Trip") == "Music/Road Trip"
    assert metadata_service.folder_for_single("Podcasts") == "Podcasts"              # explicit folder wins
    assert metadata_service.folder_for_playlist("Road Trip", "Mine") == "Mine"
    runtime_service.update(default_folder=None)
    assert metadata_service.folder_for_single() == today
    assert metadata_service.folder_for_playlist("Road Trip") == "Road Trip"


def test_api_put_and_clear(client):
    r = client.put("/api/runtime", json={"default_folder": "Music/Rock"}).json()
    assert r["default_folder"] == "Music/Rock"
    assert client.get("/api/runtime").json()["default_folder"] == "Music/Rock"
    assert client.put("/api/runtime", json={"max_parallel": 2}).json()["default_folder"] == "Music/Rock"
    assert client.put("/api/runtime", json={"default_folder": ""}).json()["default_folder"] is None
    assert client.put("/api/runtime", json={"default_folder": "x" * 300}).status_code == 422


# ───────────── progress log lines ─────────────
@pytest.mark.parametrize(
    ("mode", "pct", "last_pct", "now", "last_t", "due"),
    [
        ("off", 50, 0, 100, 0, False),
        ("milestones", 3, -1, 1, 0, True),          # first line
        ("milestones", 8, 3, 2, 1, False),          # +5 % and 1 s: quiet
        ("milestones", 13, 3, 2, 1, True),          # +10 %
        ("milestones", 4, 3, 12, 1, True),          # 10 s gap
        ("verbose", 8, 3, 2, 1, True),              # +5 %
        ("verbose", 5, 3, 2, 1, False),
        ("bogus", 50, 0, 100, 0, False),
    ],
)
def test_progress_log_due(mode, pct, last_pct, now, last_t, due):
    assert progress_log_due(mode, pct, last_pct, now, last_t) is due


def test_progress_line_format():
    line = progress_line("a1b2c3", "Believer Lyrics", 40.2, 3_250_000, 12, "downloading")
    assert line.startswith("[a1b2c3] Believer Lyrics")
    assert "40%" in line and "/s" in line and "eta" in line and "(" not in line
    assert "(video)" in progress_line("a1b2c3", "T", 5, None, None, "video")
    assert "[x] …" in progress_line("x", "", 0, None, None, "downloading")
