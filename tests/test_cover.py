"""Album art of downloaded audio: sidecar image, embedded picture (real ffmpeg), caching, errors."""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from configs import get_ffmpeg_path, get_settings
from services import library_service
from services.errors import NotFoundError

FFMPEG = get_ffmpeg_path()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings().download, "downloads_dir", tmp_path)
    return tmp_path


def _mp3(path, cover: bool):
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    if cover:
        cmd += ["-f", "lavfi", "-i", "color=c=red:s=96x96", "-frames:v", "1", "-map", "0:a", "-map", "1:v",
                "-c:v", "png", "-id3v2_version", "3"]
    cmd += ["-c:a", "libmp3lame", str(path)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        pytest.skip(f"ffmpeg cannot build the fixture: {r.stderr[-120:]!r}")


@needs_ffmpeg
def test_embedded_cover_is_extracted_as_jpeg_and_cached(root):
    (root / "Album").mkdir()
    _mp3(root / "Album" / "song.mp3", cover=True)
    data, mime = library_service.cover("Album/song.mp3")
    assert mime == "image/jpeg" and data[:2] == b"\xff\xd8"
    cached = list((root / ".covers").glob("*.jpg"))
    assert len(cached) == 1
    cached[0].write_bytes(b"\xff\xd8cached")            # a second call is served from the cache
    assert library_service.cover("Album/song.mp3")[0] == b"\xff\xd8cached"


@needs_ffmpeg
def test_no_cover_is_404_and_remembered(root):
    _mp3(root / "plain.mp3", cover=False)
    with pytest.raises(NotFoundError):
        library_service.cover("plain.mp3")
    assert list((root / ".covers").glob("*.none"))
    with pytest.raises(NotFoundError):                   # negative cache hit
        library_service.cover("plain.mp3")


def test_sidecar_image_wins(root):
    (root / "a.mp3").write_bytes(b"not really audio")
    (root / "a.jpg").write_bytes(b"\xff\xd8sidecar")
    assert library_service.cover("a.mp3") == (b"\xff\xd8sidecar", "image/jpeg")


def test_non_audio_and_missing_files(root):
    (root / "clip.mp4").write_bytes(b"x")
    with pytest.raises(NotFoundError):
        library_service.cover("clip.mp4")
    with pytest.raises(NotFoundError):
        library_service.cover("nope.mp3")


def test_cover_dir_is_hidden_from_the_library(root):
    (root / ".covers").mkdir()
    (root / ".covers" / "x.jpg").write_bytes(b"x")
    (root / "song.mp3").write_bytes(b"x")
    assert [e.name for e in library_service.list_dir("")] == ["song.mp3"]


def test_api_endpoint_and_traversal(root):
    (root / "a.mp3").write_bytes(b"x")
    (root / "a.png").write_bytes(b"\x89PNGsidecar")
    with TestClient(create_app()) as c:
        r = c.get("/api/library/cover", params={"path": "a.mp3"})
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert "max-age" in r.headers["cache-control"]
        assert c.get("/api/library/cover", params={"path": "../secret.mp3"}).status_code in (404, 422)
        assert c.get("/api/library/cover", params={"path": "missing.mp3"}).status_code == 404
