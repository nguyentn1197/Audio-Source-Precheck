"""Tests for musicflow.utils.file_utils — new helpers added in improvements pass."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from musicflow.utils.file_utils import (
    extract_archive,
    extract_archive_in_place,
    find_companions,
    is_archive_already_extracted,
    open_in_explorer,
    unique_dest,
)


# ---------------------------------------------------------------------------
# find_companions
# ---------------------------------------------------------------------------


def test_find_companions_returns_extension_matches(tmp_path: Path) -> None:
    folder = tmp_path / "album"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"F")
    (folder / "cover.jpg").write_bytes(b"I")
    (folder / "notes.txt").write_bytes(b"N")

    result = find_companions(folder, [".jpg", ".txt"])
    names = {p.name for p in result}
    assert "cover.jpg" in names
    assert "notes.txt" in names


def test_find_companions_excludes_audio_files(tmp_path: Path) -> None:
    folder = tmp_path / "album"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"F")
    (folder / "cover.jpg").write_bytes(b"I")

    result = find_companions(folder, [".jpg", ".flac"])
    names = {p.name for p in result}
    assert "01.flac" not in names   # audio must never be a companion
    assert "cover.jpg" in names


def test_find_companions_includes_subfolder_without_audio(tmp_path: Path) -> None:
    """A subfolder with no audio files is a companion folder — all its files included."""
    folder = tmp_path / "album"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"F")
    extras = folder / "Extras"
    extras.mkdir()
    (extras / "booklet.pdf").write_bytes(b"P")
    (extras / "liner.txt").write_bytes(b"L")

    result = find_companions(folder, [".pdf", ".txt"])
    names = {p.name for p in result}
    assert "booklet.pdf" in names
    assert "liner.txt" in names


def test_find_companions_excludes_subfolder_with_audio(tmp_path: Path) -> None:
    """A subfolder that contains audio is a separate disc/group — NOT a companion folder."""
    folder = tmp_path / "album"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"F")
    disc2 = folder / "Disc2"
    disc2.mkdir()
    (disc2 / "02.flac").write_bytes(b"F")
    (disc2 / "cover.jpg").write_bytes(b"I")

    result = find_companions(folder, [".jpg"])
    names = {p.name for p in result}
    # cover.jpg is inside Disc2 which has audio → must NOT be returned
    assert "cover.jpg" not in names


def test_find_companions_empty_folder(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    result = find_companions(folder, [".jpg"])
    assert result == []


def test_find_companions_nonexistent_folder(tmp_path: Path) -> None:
    result = find_companions(tmp_path / "does_not_exist", [".jpg"])
    assert result == []


def test_find_companions_empty_extensions(tmp_path: Path) -> None:
    folder = tmp_path / "album"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"F")
    (folder / "cover.jpg").write_bytes(b"I")

    result = find_companions(folder, [])
    assert result == []


def test_find_companions_deduplicates(tmp_path: Path) -> None:
    """No duplicate paths in result even if multiple discovery paths would match."""
    folder = tmp_path / "album"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"F")
    (folder / "cover.jpg").write_bytes(b"I")

    result = find_companions(folder, [".jpg", ".jpg"])  # duplicate extension
    jpg_hits = [p for p in result if p.name == "cover.jpg"]
    assert len(jpg_hits) == 1


def test_find_companions_nested_companion_folder(tmp_path: Path) -> None:
    """Files nested inside a companion subfolder are included recursively."""
    folder = tmp_path / "album"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"F")
    scans = folder / "Scans"
    scans.mkdir()
    hires = scans / "HiRes"
    hires.mkdir()
    (hires / "page1.jpg").write_bytes(b"I")

    result = find_companions(folder, [".jpg"])
    names = {p.name for p in result}
    assert "page1.jpg" in names


# ---------------------------------------------------------------------------
# Helper function for creating test ZIP files
# ---------------------------------------------------------------------------


def _make_zip(zip_path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


# ---------------------------------------------------------------------------
# open_in_explorer (smoke test — just verifies no exception on Windows)
# ---------------------------------------------------------------------------


def test_open_in_explorer_existing_folder_does_not_raise(tmp_path: Path) -> None:
    """open_in_explorer should not raise for an existing folder."""
    import platform
    if platform.system() != "Windows":
        pytest.skip("open_in_explorer is Windows-only")
    # We can't easily assert Explorer opened, but it must not raise
    open_in_explorer(tmp_path)


def test_open_in_explorer_nonexistent_path_does_not_raise(tmp_path: Path) -> None:
    """open_in_explorer must not raise even for a nonexistent path."""
    import platform
    if platform.system() != "Windows":
        pytest.skip("open_in_explorer is Windows-only")
    open_in_explorer(tmp_path / "does_not_exist")


# ---------------------------------------------------------------------------
# unique_dest (regression — ensure existing behaviour unchanged)
# ---------------------------------------------------------------------------


def test_unique_dest_returns_path_when_not_exists(tmp_path: Path) -> None:
    dst = tmp_path / "file.flac"
    assert unique_dest(dst) == dst


def test_unique_dest_appends_suffix_when_exists(tmp_path: Path) -> None:
    dst = tmp_path / "file.flac"
    dst.write_bytes(b"X")
    result = unique_dest(dst)
    assert result != dst
    assert result.stem == "file-1"


def test_unique_dest_increments_counter(tmp_path: Path) -> None:
    dst = tmp_path / "file.flac"
    dst.write_bytes(b"X")
    (tmp_path / "file-1.flac").write_bytes(b"X")
    result = unique_dest(dst)
    assert result.stem == "file-2"


def test_unique_dest_no_conflict(tmp_path: Path) -> None:
    """unique_dest should return the path unchanged if it does not exist."""
    dst = tmp_path / "track.flac"
    assert unique_dest(dst) == dst


def test_unique_dest_one_conflict(tmp_path: Path) -> None:
    """unique_dest should append -1 when destination exists."""
    dst = tmp_path / "track.flac"
    dst.touch()
    result = unique_dest(dst)
    assert result == tmp_path / "track-1.flac"


def test_unique_dest_two_conflicts(tmp_path: Path) -> None:
    """unique_dest should append -2 when -1 also exists."""
    dst = tmp_path / "track.flac"
    dst.touch()
    (tmp_path / "track-1.flac").touch()
    result = unique_dest(dst)
    assert result == tmp_path / "track-2.flac"


# ---------------------------------------------------------------------------
# is_archive_already_extracted (new universal archive helper)
# ---------------------------------------------------------------------------


def test_is_archive_already_extracted_false_when_no_sibling(tmp_path: Path) -> None:
    """Archive with no sibling dir → not extracted."""
    archive = tmp_path / "Album.tar.gz"
    archive.write_bytes(b"TAR")
    assert not is_archive_already_extracted(archive)


def test_is_archive_already_extracted_false_when_sibling_has_no_audio(tmp_path: Path) -> None:
    """Sibling dir exists but contains no audio → not extracted."""
    archive = tmp_path / "Album.7z"
    archive.write_bytes(b"7Z")
    sibling = tmp_path / "Album"
    sibling.mkdir()
    (sibling / "readme.txt").write_bytes(b"T")
    assert not is_archive_already_extracted(archive)


def test_is_archive_already_extracted_true_when_sibling_has_audio(tmp_path: Path) -> None:
    """Sibling dir has audio → extracted."""
    archive = tmp_path / "Album.rar"
    archive.write_bytes(b"RAR")
    sibling = tmp_path / "Album"
    sibling.mkdir()
    (sibling / "01.flac").write_bytes(b"F")
    assert is_archive_already_extracted(archive)


def test_is_archive_already_extracted_handles_tar_gz_extension(tmp_path: Path) -> None:
    """Multi-part extension .tar.gz should be handled correctly."""
    archive = tmp_path / "Album.tar.gz"
    archive.write_bytes(b"TAR")
    sibling = tmp_path / "Album"
    sibling.mkdir()
    (sibling / "01.mp3").write_bytes(b"M")
    assert is_archive_already_extracted(archive)


# ---------------------------------------------------------------------------
# extract_archive_in_place (new universal archive helper)
# ---------------------------------------------------------------------------


def test_extract_archive_in_place_creates_sibling_dir_for_zip(tmp_path: Path) -> None:
    """ZIP extraction creates sibling dir."""
    z = tmp_path / "Album.zip"
    _make_zip(z, {"01.flac": b"FAKE"})
    dest = extract_archive_in_place(z)
    assert dest == tmp_path / "Album"
    assert dest.is_dir()
    assert (dest / "01.flac").exists()


def test_extract_archive_in_place_is_idempotent_for_zip(tmp_path: Path) -> None:
    """Calling extract_archive_in_place twice on ZIP is idempotent."""
    z = tmp_path / "Album.zip"
    _make_zip(z, {"01.flac": b"FAKE"})
    dest1 = extract_archive_in_place(z)
    dest2 = extract_archive_in_place(z)
    assert dest1 == dest2
    flac_files = list(dest1.glob("*.flac"))
    assert len(flac_files) == 1


def test_extract_archive_in_place_skips_when_audio_present_zip(tmp_path: Path) -> None:
    """ZIP pre-extraction guard: skip if sibling already has audio."""
    z = tmp_path / "Album.zip"
    _make_zip(z, {"01.flac": b"NEW_CONTENT"})
    sibling = tmp_path / "Album"
    sibling.mkdir()
    existing = sibling / "01.flac"
    existing.write_bytes(b"ORIGINAL_CONTENT")
    extract_archive_in_place(z)
    # Content must be original, not overwritten
    assert existing.read_bytes() == b"ORIGINAL_CONTENT"


def test_extract_archive_in_place_handles_tar_gz_extension(tmp_path: Path) -> None:
    """TAR.GZ extension should be stripped correctly (not just .gz)."""
    import tarfile
    tar_path = tmp_path / "Album.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        import io
        info = tarfile.TarInfo(name="01.flac")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"FAKE"))
    
    dest = extract_archive_in_place(tar_path)
    assert dest == tmp_path / "Album"
    assert (dest / "01.flac").exists()


# ---------------------------------------------------------------------------
# extract_archive (new universal extraction function)
# ---------------------------------------------------------------------------


def test_extract_archive_zip_to_dest(tmp_path: Path) -> None:
    """extract_archive extracts ZIP to a specific destination."""
    z = tmp_path / "Album.zip"
    _make_zip(z, {"01.flac": b"FAKE", "cover.jpg": b"IMG"})
    dest = tmp_path / "output"
    extracted = extract_archive(z, dest)
    
    assert dest.is_dir()
    assert (dest / "01.flac").exists()
    assert (dest / "cover.jpg").exists()
    assert all(p.is_file() for p in extracted)


def test_extract_archive_returns_all_files(tmp_path: Path) -> None:
    """extract_archive returns list of all extracted files."""
    z = tmp_path / "Album.zip"
    _make_zip(z, {"01.flac": b"F", "02.flac": b"F", "cover.jpg": b"I"})
    dest = tmp_path / "output"
    extracted = extract_archive(z, dest)
    
    names = {p.name for p in extracted}
    assert "01.flac" in names
    assert "02.flac" in names
    assert "cover.jpg" in names
    assert len(extracted) == 3


def test_extract_archive_tar_gz_to_dest(tmp_path: Path) -> None:
    """extract_archive handles TAR.GZ format."""
    import tarfile
    import io
    
    tar_path = tmp_path / "Album.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        info = tarfile.TarInfo(name="01.flac")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"FAKE"))
        info2 = tarfile.TarInfo(name="cover.jpg")
        info2.size = 3
        tf.addfile(info2, io.BytesIO(b"IMG"))
    
    dest = tmp_path / "output"
    extracted = extract_archive(tar_path, dest)
    
    assert dest.is_dir()
    assert (dest / "01.flac").exists()
    assert (dest / "cover.jpg").exists()
    assert len(extracted) == 2
