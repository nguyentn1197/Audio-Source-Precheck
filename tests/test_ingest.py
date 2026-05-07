"""Tests for musicflow.core.ingest."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from musicflow.core.ingest import (
    IngestResult,
    SourceItem,
    SourceType,
    ingest_files,
    scan_source,
)
from musicflow.utils.file_utils import AUDIO_EXTENSIONS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def torrent_dir(tmp_path: Path) -> Path:
    """Create a fake Torrent source folder with audio files and a ZIP."""
    src = tmp_path / "torrent_src"
    src.mkdir()

    # Flat audio file
    (src / "track01.flac").write_bytes(b"FAKEFLAC")

    # Subfolder
    album_dir = src / "Artist - Album"
    album_dir.mkdir()
    (album_dir / "01 - Song.mp3").write_bytes(b"FAKEMP3")
    (album_dir / "02 - Song.mp3").write_bytes(b"FAKEMP3")

    # ZIP containing audio
    zip_path = src / "bonus.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("bonus_track.flac", b"FAKEBONUS")

    return src


@pytest.fixture()
def soulseek_dir(tmp_path: Path) -> Path:
    """Create a fake Soulseek source folder."""
    src = tmp_path / "slsk_src"
    src.mkdir()
    (src / "slsk_track.mp3").write_bytes(b"FAKESLSK")
    return src


@pytest.fixture()
def staging(tmp_path: Path) -> Path:
    d = tmp_path / "staging1"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# scan_source
# ---------------------------------------------------------------------------


def test_scan_source_finds_audio_and_zips(torrent_dir: Path) -> None:
    items = scan_source(torrent_dir, SourceType.TORRENT)
    names = {i.path.name for i in items}
    assert "track01.flac" in names
    assert "01 - Song.mp3" in names
    assert "02 - Song.mp3" in names
    assert "bonus.zip" in names


def test_scan_source_marks_archive(torrent_dir: Path) -> None:
    items = scan_source(torrent_dir, SourceType.TORRENT)
    archive_items = [i for i in items if i.is_archive]
    assert len(archive_items) == 1
    assert archive_items[0].path.name == "bonus.zip"


def test_scan_source_nonexistent_folder(tmp_path: Path) -> None:
    items = scan_source(tmp_path / "does_not_exist", SourceType.TORRENT)
    assert items == []


# ---------------------------------------------------------------------------
# ingest_files — Torrent (copy)
# ---------------------------------------------------------------------------


def test_torrent_files_are_copied(torrent_dir: Path, staging: Path) -> None:
    items = [i for i in scan_source(torrent_dir, SourceType.TORRENT) if not i.is_archive]
    result = ingest_files(items, torrent_dir, staging)

    assert result.error_count == 0
    assert result.success_count == len(items)

    # Originals must still exist
    for item in items:
        assert item.path.exists(), f"Original was deleted: {item.path}"

    # Copies must exist in staging
    for staged in result.staged:
        assert staged.path.exists()


# ---------------------------------------------------------------------------
# ingest_files — Soulseek (move)
# ---------------------------------------------------------------------------


def test_soulseek_files_are_moved(soulseek_dir: Path, staging: Path) -> None:
    items = scan_source(soulseek_dir, SourceType.SOULSEEK)
    originals = [i.path for i in items]
    result = ingest_files(items, soulseek_dir, staging)

    assert result.error_count == 0
    assert result.success_count == len(items)

    # Originals must be gone
    for orig in originals:
        assert not orig.exists(), f"Original still exists after move: {orig}"

    # Files must be in staging
    for staged in result.staged:
        assert staged.path.exists()


# ---------------------------------------------------------------------------
# ingest_files — ZIP extraction
# ---------------------------------------------------------------------------


def test_archive_is_extracted(torrent_dir: Path, staging: Path) -> None:
    archive_items = [i for i in scan_source(torrent_dir, SourceType.TORRENT) if i.is_archive]
    result = ingest_files(archive_items, torrent_dir, staging)

    assert result.error_count == 0
    # Archive should have been extracted to a subfolder
    extracted = list(staging.rglob("*.flac"))
    assert any("bonus_track" in p.name for p in extracted)

    # Original archive must still exist (torrent — copy rule applies to archives too)
    for item in archive_items:
        assert item.path.exists()


def test_soulseek_archive_deleted_after_extraction(tmp_path: Path, staging: Path) -> None:
    slsk = tmp_path / "slsk"
    slsk.mkdir()
    archive_path = slsk / "album.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("song.flac", b"FAKE")

    items = scan_source(slsk, SourceType.SOULSEEK)
    ingest_files(items, slsk, staging)

    assert not archive_path.exists(), "Soulseek archive should be deleted after extraction"


# ---------------------------------------------------------------------------
# Progress callback
# ---------------------------------------------------------------------------


def test_progress_callback_called(torrent_dir: Path, staging: Path) -> None:
    items = [i for i in scan_source(torrent_dir, SourceType.TORRENT) if not i.is_archive]
    calls: list[tuple[int, int, str, bool]] = []
    ingest_files(items, torrent_dir, staging, progress_cb=lambda c, t, n, e: calls.append((c, t, n, e)))
    assert len(calls) > 0
    # Last call should be (total, total, "Done", False)
    assert calls[-1][0] == calls[-1][1]
    assert calls[-1][3] == False  # is_error should be False for final "Done" call


# ---------------------------------------------------------------------------
# Companion file carry-along
# ---------------------------------------------------------------------------


def test_companion_files_copied_with_torrent_audio(tmp_path: Path) -> None:
    """Companion files (cover art, booklets) are copied alongside Torrent audio."""
    src = tmp_path / "torrent"
    src.mkdir()
    album = src / "Artist - Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"FAKE")
    (album / "cover.jpg").write_bytes(b"IMG")       # companion file at audio level
    extras = album / "Extras"
    extras.mkdir()
    (extras / "booklet.pdf").write_bytes(b"PDF")    # companion subfolder (no audio inside)

    staging = tmp_path / "staging"
    staging.mkdir()

    items = scan_source(src, SourceType.TORRENT, companion_extensions=[".jpg", ".pdf"])
    result = ingest_files(
        items, src, staging, companion_extensions=[".jpg", ".pdf"]
    )

    staged_names = {s.path.name for s in result.staged}
    assert "cover.jpg" in staged_names
    assert "booklet.pdf" in staged_names

    # Torrent originals must still exist
    assert (album / "cover.jpg").exists()
    assert (extras / "booklet.pdf").exists()

    # Audio file staged as non-companion
    audio_staged = [s for s in result.staged if not s.is_companion]
    assert any("01.flac" in s.path.name for s in audio_staged)


def test_companion_files_moved_with_soulseek_audio(tmp_path: Path) -> None:
    """Companion files are moved (deleted from source) alongside Soulseek audio."""
    src = tmp_path / "slsk"
    src.mkdir()
    album = src / "Artist - Album"
    album.mkdir()
    (album / "01.mp3").write_bytes(b"FAKE")
    cover = album / "cover.jpg"
    cover.write_bytes(b"IMG")

    staging = tmp_path / "staging"
    staging.mkdir()

    items = scan_source(src, SourceType.SOULSEEK, companion_extensions=[".jpg"])
    result = ingest_files(items, src, staging, companion_extensions=[".jpg"])

    staged_names = {s.path.name for s in result.staged}
    assert "cover.jpg" in staged_names

    # Soulseek original companion must be gone
    assert not cover.exists()


def test_companion_subfolder_with_audio_not_included(tmp_path: Path) -> None:
    """A subfolder that contains audio files is NOT treated as a companion folder.
    
    However, files inside that subfolder ARE companions to the audio in that subfolder.
    """
    src = tmp_path / "torrent"
    src.mkdir()
    album = src / "Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"FAKE")
    disc2 = album / "Disc2"
    disc2.mkdir()
    (disc2 / "02.flac").write_bytes(b"FAKE")   # audio → Disc2 is not a companion folder
    (disc2 / "cover.jpg").write_bytes(b"IMG")
    
    # But add a file at Album level that should NOT be found (it's in a subfolder with audio)
    (album / "album_cover.jpg").write_bytes(b"IMG")  # This SHOULD be a companion to 01.flac

    staging = tmp_path / "staging"
    staging.mkdir()

    items = scan_source(src, SourceType.TORRENT, companion_extensions=[".jpg"])
    companion_items = [i for i in items if i.is_companion]
    companion_names = {i.path.name for i in companion_items}
    
    # cover.jpg inside Disc2 IS a companion (to 02.flac in that folder)
    assert "cover.jpg" in companion_names
    # album_cover.jpg at Album level IS a companion (to 01.flac)
    assert "album_cover.jpg" in companion_names


def test_duplicate_cover_art_gets_unique_name(tmp_path: Path) -> None:
    """Files organized by source type don't conflict even with same name."""
    src_t = tmp_path / "torrent"
    src_t.mkdir()
    album_t = src_t / "Album"
    album_t.mkdir()
    (album_t / "01.flac").write_bytes(b"FAKE")
    (album_t / "cover.jpg").write_bytes(b"TORRENT_COVER")

    src_s = tmp_path / "slsk"
    src_s.mkdir()
    album_s = src_s / "Album"
    album_s.mkdir()
    (album_s / "01.flac").write_bytes(b"FAKE")
    (album_s / "cover.jpg").write_bytes(b"SLSK_COVER")

    staging = tmp_path / "staging"
    staging.mkdir()

    # Ingest Torrent first
    t_items = scan_source(src_t, SourceType.TORRENT, companion_extensions=[".jpg"])
    result_t = ingest_files(t_items, src_t, staging, companion_extensions=[".jpg"])

    # Ingest Soulseek second — cover.jpg no longer conflicts (different source folder)
    s_items = scan_source(src_s, SourceType.SOULSEEK, companion_extensions=[".jpg"])
    result_s = ingest_files(s_items, src_s, staging, companion_extensions=[".jpg"])

    # Get companion files from both results
    torrent_companion = [s for s in result_t.staged if s.is_companion]
    soulseek_companion = [s for s in result_s.staged if s.is_companion]
    
    assert torrent_companion, "Torrent cover.jpg should have been staged"
    assert soulseek_companion, "Soulseek cover.jpg should have been staged"
    
    # With source folder organization, files don't conflict
    # Both should keep original names in their respective source folders
    assert soulseek_companion[0].path.name == "cover.jpg"
    assert torrent_companion[0].path.name == "cover.jpg"
    
    # Verify they're in different source folders
    assert "soulseek" in str(soulseek_companion[0].path)
    assert "torrent" in str(torrent_companion[0].path)
    
    # Both files should exist
    assert soulseek_companion[0].path.exists()
    assert torrent_companion[0].path.exists()


# ---------------------------------------------------------------------------
# ZIP pre-extraction idempotency
# ---------------------------------------------------------------------------


def test_archive_pre_extraction_idempotent(tmp_path: Path) -> None:
    """Scanning the same source twice must not duplicate audio items from an archive."""
    src = tmp_path / "torrent"
    src.mkdir()
    archive_path = src / "Album.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("01.flac", b"FAKE")

    # First scan — extracts the archive in-place
    items1 = scan_source(src, SourceType.TORRENT)
    audio1 = [i for i in items1 if not i.is_archive and not i.is_companion]

    # Second scan — archive already extracted; must not produce duplicates
    items2 = scan_source(src, SourceType.TORRENT)
    audio2 = [i for i in items2 if not i.is_archive and not i.is_companion]

    assert len(audio1) == 1
    assert len(audio2) == 1
    assert audio1[0].path == audio2[0].path


def test_archive_pre_extracted_audio_appears_in_scan(tmp_path: Path) -> None:
    """Audio extracted from an archive shows up as a regular SourceItem after scan."""
    src = tmp_path / "torrent"
    src.mkdir()
    archive_path = src / "Album.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("track01.flac", b"FAKE")
        zf.writestr("track02.flac", b"FAKE")

    items = scan_source(src, SourceType.TORRENT)
    audio = [i for i in items if not i.is_archive and not i.is_companion]
    assert len(audio) == 2
    names = {i.path.name for i in audio}
    assert "track01.flac" in names
    assert "track02.flac" in names


# ---------------------------------------------------------------------------
# Archive extraction (TAR.GZ, TAR.BZ2, etc.)
# ---------------------------------------------------------------------------


def test_tar_gz_archive_extraction(tmp_path: Path, staging: Path) -> None:
    """TAR.GZ archives are correctly scanned and extracted."""
    import tarfile
    import io
    
    src = tmp_path / "torrent"
    src.mkdir()
    
    # Create a TAR.GZ archive
    tar_path = src / "Album.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        info = tarfile.TarInfo(name="01.flac")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"FAKE"))
        info2 = tarfile.TarInfo(name="02.flac")
        info2.size = 4
        tf.addfile(info2, io.BytesIO(b"FAKE"))
    
    items = scan_source(src, SourceType.TORRENT)
    archive_items = [i for i in items if i.is_archive]
    assert len(archive_items) == 1
    assert archive_items[0].path.name == "Album.tar.gz"
    
    # Ingest the archive
    result = ingest_files(archive_items, src, staging)
    assert result.error_count == 0
    
    # Original TAR.GZ must still exist (torrent copy rule)
    assert tar_path.exists()
    
    # Audio files should be extracted
    extracted = list(staging.rglob("*.flac"))
    assert len(extracted) == 2


def test_tar_bz2_archive_extraction(tmp_path: Path, staging: Path) -> None:
    """TAR.BZ2 archives are correctly scanned and extracted."""
    import tarfile
    import io
    
    src = tmp_path / "torrent"
    src.mkdir()
    
    # Create a TAR.BZ2 archive
    tar_path = src / "Album.tar.bz2"
    with tarfile.open(tar_path, "w:bz2") as tf:
        info = tarfile.TarInfo(name="song.mp3")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"FAKE"))
    
    items = scan_source(src, SourceType.TORRENT)
    archive_items = [i for i in items if i.is_archive]
    assert len(archive_items) == 1
    
    result = ingest_files(archive_items, src, staging)
    assert result.error_count == 0
    assert tar_path.exists()


def test_soulseek_tar_gz_deleted_after_extraction(tmp_path: Path, staging: Path) -> None:
    """TAR.GZ from Soulseek is deleted after extraction."""
    import tarfile
    import io
    
    slsk = tmp_path / "slsk"
    slsk.mkdir()
    
    tar_path = slsk / "Album.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        info = tarfile.TarInfo(name="01.flac")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"FAKE"))
    
    items = scan_source(slsk, SourceType.SOULSEEK)
    ingest_files(items, slsk, staging)
    
    assert not tar_path.exists(), "Soulseek TAR.GZ should be deleted after extraction"


def test_archive_with_companion_files(tmp_path: Path, staging: Path) -> None:
    """Archive extraction with companion files inside the archive."""
    src = tmp_path / "torrent"
    src.mkdir()
    
    # Create archive with audio and companion files
    zip_path = src / "Album.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("01.flac", b"FAKE")
        zf.writestr("cover.jpg", b"IMG")  # companion inside the archive
    
    items = scan_source(src, SourceType.TORRENT, companion_extensions=[".jpg"])
    result = ingest_files(items, src, staging, companion_extensions=[".jpg"])
     
    # Both audio and companion should be staged
    staged_names = {s.path.name for s in result.staged}
    assert any("01.flac" in n for n in staged_names)
    assert "cover.jpg" in staged_names


def test_companion_lands_in_same_folder_as_audio(tmp_path: Path) -> None:
    """Companion files must end up in the same staging folder as the audio file."""
    src = tmp_path / "torrent"
    src.mkdir()
    album = src / "Pink Floyd - The Wall"
    album.mkdir()
    (album / "01 - In The Flesh.flac").write_bytes(b"FAKEFLAC")
    (album / "cover.jpg").write_bytes(b"IMG")

    staging = tmp_path / "staging"
    staging.mkdir()

    items = scan_source(src, SourceType.TORRENT, companion_extensions=[".jpg"])
    result = ingest_files(items, src, staging, companion_extensions=[".jpg"])

    audio_staged = [s for s in result.staged if not s.is_companion]
    companion_staged = [s for s in result.staged if s.is_companion]

    assert audio_staged, "Audio file should be staged"
    assert companion_staged, "Companion file should be staged"

    # Companion must be in the same directory as the audio file
    assert companion_staged[0].path.parent == audio_staged[0].path.parent, (
        f"Companion in {companion_staged[0].path.parent}, "
        f"audio in {audio_staged[0].path.parent}"
    )
