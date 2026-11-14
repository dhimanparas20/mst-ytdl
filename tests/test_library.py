"""services/library.py — path safety, listing, zip streaming, deletion."""

from __future__ import annotations

import io
import zipfile

import pytest

from services import InvalidRequestError, NotFoundError
from services import library_service as lib


@pytest.fixture
def tree(downloads_dir):
    (downloads_dir / "2026-01-01").mkdir()
    (downloads_dir / "2026-01-01" / "a.mp3").write_bytes(b"a" * 100)
    (downloads_dir / "2026-01-01" / "b.mp4").write_bytes(b"b" * 300)
    (downloads_dir / "2026-01-01" / "b.mp4.part").write_bytes(b"partial")  # hidden
    (downloads_dir / "My Mix").mkdir()
    (downloads_dir / "My Mix" / "01 - x.mp3").write_bytes(b"x" * 50)
    (downloads_dir / "My Mix" / "nested").mkdir()
    (downloads_dir / "My Mix" / "nested" / "02 - y.mp3").write_bytes(b"y" * 25)
    (downloads_dir / "loose.mp3").write_bytes(b"l" * 10)
    (downloads_dir / ".gitkeep").write_bytes(b"")
    return downloads_dir


class TestSafePath:
    @pytest.mark.parametrize("rel", ["", "2026-01-01", "2026-01-01/a.mp3", "My Mix/nested"])
    def test_inside_is_resolved(self, tree, rel):
        p = lib.safe_path(rel)
        assert tree in p.parents or p == tree

    @pytest.mark.parametrize("rel", [
        "..", "../", "../x", "2026-01-01/../../x", "a/../../..", "..\\..\\windows", "%2e%2e/x" if False else "../../etc/passwd",
        "C:\\Windows\\System32", "/etc/passwd", "\\\\server\\share\\x",
    ])
    def test_escapes_are_rejected(self, tree, rel):
        with pytest.raises(InvalidRequestError):
            lib.safe_path(rel)

    def test_sibling_directory_with_same_prefix_is_rejected(self, tree):
        sibling = tree.parent / (tree.name + "-evil")
        sibling.mkdir()
        with pytest.raises(InvalidRequestError):
            lib.safe_path(f"../{sibling.name}")

    def test_symlink_escape_is_rejected(self, tree, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("s")
        link = tree / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted on this system")
        with pytest.raises(InvalidRequestError):
            lib.safe_path("link/secret.txt")


class TestListing:
    def test_root_lists_folders_first_with_aggregates(self, tree):
        entries = lib.list_dir("")
        names = [e.name for e in entries]
        assert names[:2] in (["2026-01-01", "My Mix"], ["My Mix", "2026-01-01"])
        assert names[-1] == "loose.mp3" and ".gitkeep" not in names
        by = {e.name: e for e in entries}
        assert by["2026-01-01"].is_dir and by["2026-01-01"].files == 2 and by["2026-01-01"].size == 400  # .part ignored
        assert by["My Mix"].files == 2 and by["My Mix"].size == 75  # recursive
        assert by["loose.mp3"].ext == "mp3" and by["loose.mp3"].size == 10 and not by["loose.mp3"].is_dir

    def test_subfolder_listing_hides_partials(self, tree):
        names = {e.name for e in lib.list_dir("2026-01-01")}
        assert names == {"a.mp3", "b.mp4"}

    def test_paths_are_posix_relative(self, tree):
        e = next(e for e in lib.list_dir("My Mix") if e.name == "nested")
        assert e.path == "My Mix/nested"

    def test_missing_folder(self, tree):
        with pytest.raises(NotFoundError):
            lib.list_dir("nope")

    def test_file_is_not_a_folder(self, tree):
        with pytest.raises(InvalidRequestError):
            lib.list_dir("loose.mp3")

    def test_missing_root_is_an_empty_listing(self, tmp_path, monkeypatch):
        from configs import get_settings

        monkeypatch.setattr(get_settings().download, "downloads_dir", (tmp_path / "never").resolve())
        assert lib.list_dir("") == []
        assert lib.usage() == {"files": 0, "bytes": 0}

    def test_usage_counts_real_files_only(self, tree):
        assert lib.usage() == {"files": 5, "bytes": 100 + 300 + 50 + 25 + 10}


class TestGetFile:
    def test_returns_path(self, tree):
        assert lib.get_file("2026-01-01/a.mp3").read_bytes() == b"a" * 100

    @pytest.mark.parametrize("rel", ["nope.mp3", "2026-01-01", ""])
    def test_not_a_file(self, tree, rel):
        with pytest.raises(NotFoundError):
            lib.get_file(rel)

    def test_traversal(self, tree):
        with pytest.raises(InvalidRequestError):
            lib.get_file("../x")


class TestZip:
    def read_zip(self, chunks) -> zipfile.ZipFile:
        return zipfile.ZipFile(io.BytesIO(b"".join(chunks)))

    def test_zip_contains_files_with_relative_names_and_content(self, tree):
        name, chunks = lib.zip_folder("My Mix")
        assert name == "My Mix.zip"
        with self.read_zip(chunks) as z:
            assert z.testzip() is None
            assert sorted(z.namelist()) == ["01 - x.mp3", "nested/02 - y.mp3"]
            assert z.read("01 - x.mp3") == b"x" * 50 and z.read("nested/02 - y.mp3") == b"y" * 25

    def test_partials_are_excluded(self, tree):
        _, chunks = lib.zip_folder("2026-01-01")
        with self.read_zip(chunks) as z:
            assert sorted(z.namelist()) == ["a.mp3", "b.mp4"]

    def test_large_file_streams_in_multiple_chunks(self, tree):
        big = tree / "Big"
        big.mkdir()
        (big / "f.bin").write_bytes(b"z" * (3 * 1024 * 1024 + 7))
        _, chunks = lib.zip_folder("Big")
        parts = list(chunks)
        assert len(parts) > 3  # produced incrementally, not as one blob
        with self.read_zip(parts) as z:
            assert len(z.read("f.bin")) == 3 * 1024 * 1024 + 7

    def test_unicode_names(self, tree):
        d = tree / "日本"
        d.mkdir()
        (d / "曲 🎵.mp3").write_bytes(b"1")
        with self.read_zip(lib.zip_folder("日本")[1]) as z:
            assert z.namelist() == ["曲 🎵.mp3"]

    def test_empty_or_missing_folder(self, tree):
        (tree / "empty").mkdir()
        with pytest.raises(NotFoundError):
            lib.zip_folder("empty")
        with pytest.raises(NotFoundError):
            lib.zip_folder("ghost")

    def test_zipping_a_file_is_refused(self, tree):
        with pytest.raises(NotFoundError):
            lib.zip_folder("loose.mp3")

    def test_traversal(self, tree):
        with pytest.raises(InvalidRequestError):
            lib.zip_folder("..")


class TestDelete:
    def test_delete_file(self, tree):
        assert lib.delete("loose.mp3") == 1 and not (tree / "loose.mp3").exists()

    def test_delete_folder_recursively(self, tree):
        assert lib.delete("My Mix") == 2
        assert not (tree / "My Mix").exists() and (tree / "2026-01-01").exists()

    def test_cannot_delete_root(self, tree):
        with pytest.raises(InvalidRequestError, match="root"):
            lib.delete("")
        with pytest.raises(InvalidRequestError):
            lib.delete(".")
        assert tree.exists()

    def test_missing(self, tree):
        with pytest.raises(NotFoundError):
            lib.delete("ghost")

    def test_traversal(self, tree, tmp_path):
        victim = tmp_path / "victim.txt"
        victim.write_text("x")
        with pytest.raises(InvalidRequestError):
            lib.delete("../victim.txt")
        assert victim.exists()
