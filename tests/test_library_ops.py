"""LibraryService bulk operations: multi-zip, bulk delete, move, rename, folder list."""

from __future__ import annotations

import io
import zipfile

import pytest

from services import ConflictError, InvalidRequestError, NotFoundError
from services import library_service as lib


@pytest.fixture
def tree(downloads_dir):
    (downloads_dir / "2026-01-01").mkdir()
    (downloads_dir / "2026-01-01" / "a.mp3").write_bytes(b"a" * 10)
    (downloads_dir / "2026-01-01" / "b.mp3").write_bytes(b"b" * 20)
    (downloads_dir / "2026-01-01" / "b.mp3.part").write_bytes(b"partial")
    (downloads_dir / "Mix").mkdir()
    (downloads_dir / "Mix" / "01 - x.mp3").write_bytes(b"x" * 30)
    (downloads_dir / "Mix" / "deep").mkdir()
    (downloads_dir / "Mix" / "deep" / "02 - y.mp3").write_bytes(b"y" * 40)
    (downloads_dir / "loose.mp4").write_bytes(b"l" * 50)
    return downloads_dir


def names(chunks):
    with zipfile.ZipFile(io.BytesIO(b"".join(chunks))) as z:
        assert z.testzip() is None
        return sorted(z.namelist())


class TestZipPaths:
    def test_single_folder_matches_zip_folder(self, tree):
        name, chunks = lib.zip_paths(["Mix"])
        assert name == "Mix.zip" and names(chunks) == ["01 - x.mp3", "deep/02 - y.mp3"]

    def test_mixed_selection_keeps_root_relative_names(self, tree):
        name, chunks = lib.zip_paths(["Mix", "loose.mp4", "2026-01-01/a.mp3"])
        assert name == "selection.zip"
        assert names(chunks) == ["2026-01-01/a.mp3", "Mix/01 - x.mp3", "Mix/deep/02 - y.mp3", "loose.mp4"]

    def test_two_folders_stay_separate(self, tree):
        _, chunks = lib.zip_paths(["2026-01-01", "Mix"])
        got = names(chunks)
        assert "2026-01-01/a.mp3" in got and "Mix/01 - x.mp3" in got and not any(n.endswith(".part") for n in got)

    def test_single_file(self, tree):
        name, chunks = lib.zip_paths(["loose.mp4"])
        assert name == "selection.zip" and names(chunks) == ["loose.mp4"]

    def test_duplicates_and_overlap_are_deduped(self, tree):
        _, chunks = lib.zip_paths(["Mix", "Mix", "Mix/01 - x.mp3"])
        got = names(chunks)
        assert got.count("Mix/01 - x.mp3") == 1

    def test_errors(self, tree):
        with pytest.raises(InvalidRequestError):
            lib.zip_paths([])
        with pytest.raises(NotFoundError):
            lib.zip_paths(["Mix", "ghost"])
        with pytest.raises(InvalidRequestError):
            lib.zip_paths(["Mix", "../x"])
        (tree / "empty").mkdir()
        with pytest.raises(NotFoundError):
            lib.zip_paths(["empty"])


class TestDeleteMany:
    def test_deletes_files_and_folders_and_reports_failures(self, tree):
        r = lib.delete_many(["loose.mp4", "Mix", "ghost", "../x"])
        assert r["deleted"] == 2 and r["files"] == 3
        assert sorted(e["path"] for e in r["errors"]) == ["../x", "ghost"]
        assert not (tree / "Mix").exists() and not (tree / "loose.mp4").exists()

    def test_root_is_protected_in_bulk_too(self, tree):
        r = lib.delete_many([""])
        assert r["deleted"] == 0 and r["errors"] and tree.exists()

    def test_duplicates(self, tree):
        r = lib.delete_many(["loose.mp4", "loose.mp4"])
        assert r["deleted"] == 1 and r["errors"] == []


class TestMove:
    def test_move_into_existing_and_new_nested_folder(self, tree):
        r = lib.move(["loose.mp4"], "Mix")
        assert r["moved"] == 1 and (tree / "Mix" / "loose.mp4").exists()
        r = lib.move(["2026-01-01/a.mp3", "2026-01-01/b.mp3"], "Music/Rock")
        assert r == {"moved": 2, "dest": "Music/Rock", "errors": []}
        assert (tree / "Music" / "Rock" / "a.mp3").exists()

    def test_move_folder(self, tree):
        lib.move(["Mix"], "Archive")
        assert (tree / "Archive" / "Mix" / "deep" / "02 - y.mp3").exists() and not (tree / "Mix").exists()

    def test_never_overwrites(self, tree):
        (tree / "Mix" / "a.mp3").write_bytes(b"existing")
        r = lib.move(["2026-01-01/a.mp3"], "Mix")
        assert r["moved"] == 0 and "already exists" in r["errors"][0]["error"]
        assert (tree / "Mix" / "a.mp3").read_bytes() == b"existing" and (tree / "2026-01-01" / "a.mp3").exists()

    def test_cannot_move_folder_into_itself_or_child(self, tree):
        for dest in ("Mix", "Mix/deep"):
            r = lib.move(["Mix"], dest)
            assert r["moved"] == 0 and "into itself" in r["errors"][0]["error"]
        assert (tree / "Mix" / "01 - x.mp3").exists()

    def test_destination_is_sanitised(self, tree):
        r = lib.move(["loose.mp4"], "../../outside")
        assert r["dest"] == "outside" and (tree / "outside" / "loose.mp4").exists()
        assert not (tree.parent / "outside").exists()

    def test_move_to_root(self, tree):
        lib.move(["Mix/deep"], "")
        assert (tree / "deep" / "02 - y.mp3").exists()

    def test_moving_where_it_already_is_is_a_noop(self, tree):
        r = lib.move(["Mix/01 - x.mp3"], "Mix")
        assert r["moved"] == 0 and r["errors"] == []

    def test_errors_do_not_abort_the_batch(self, tree):
        r = lib.move(["ghost", "../x", "loose.mp4"], "Mix")
        assert r["moved"] == 1 and len(r["errors"]) == 2

    def test_root_cannot_be_moved(self, tree):
        assert lib.move([""], "Mix")["errors"]


class TestRename:
    def test_rename_file_keeps_extension(self, tree):
        e = lib.rename("loose.mp4", "My Clip")
        assert e.name == "My Clip.mp4" and (tree / "My Clip.mp4").exists() and not (tree / "loose.mp4").exists()

    def test_rename_with_explicit_extension(self, tree):
        assert lib.rename("loose.mp4", "x.mkv").name == "x.mkv"

    def test_rename_folder(self, tree):
        e = lib.rename("Mix", "Road Trip")
        assert e.is_dir and e.name == "Road Trip" and (tree / "Road Trip" / "01 - x.mp3").exists()

    def test_name_is_sanitised_no_path_injection(self, tree):
        e = lib.rename("loose.mp4", "../evil/name")
        assert e.name == "evilname.mp4" and (tree / "evilname.mp4").exists()
        assert e.path == "evilname.mp4"

    def test_clash_with_existing_name(self, tree):
        (tree / "taken.mp4").write_bytes(b"t")
        with pytest.raises(ConflictError):
            lib.rename("loose.mp4", "taken")

    def test_missing_root_and_empty(self, tree):
        with pytest.raises(NotFoundError):
            lib.rename("ghost", "x")
        with pytest.raises(InvalidRequestError):
            lib.rename("", "x")
        with pytest.raises(InvalidRequestError):
            lib.rename("loose.mp4", "///")
        with pytest.raises(InvalidRequestError):
            lib.rename("../x", "y")

    def test_same_name_is_a_noop(self, tree):
        assert lib.rename("loose.mp4", "loose.mp4").name == "loose.mp4"


class TestListFolders:
    def test_lists_nested_folders_shallow_first(self, tree):
        assert lib.list_folders() == ["2026-01-01", "Mix", "Mix/deep"]

    def test_depth_cap(self, tree):
        (tree / "a" / "b" / "c" / "d").mkdir(parents=True)
        got = lib.list_folders(max_depth=2)
        assert "a/b" in got and "a/b/c" not in got

    def test_empty_or_missing_root(self, tmp_path, monkeypatch):
        from configs import get_settings

        monkeypatch.setattr(get_settings().download, "downloads_dir", (tmp_path / "none").resolve())
        assert lib.list_folders() == []
