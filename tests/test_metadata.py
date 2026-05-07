"""Tests for musicflow.core.metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from musicflow.core.metadata import (
    AlbumKey,
    TrackMetadata,
    group_by_album,
    read_metadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_track(
    path: str = "track.flac",
    artist: str | None = "Artist",
    album: str | None = "Album",
    album_artist: str | None = None,
    date: str | None = "2024",
    mb_album_id: str | None = None,
) -> TrackMetadata:
    return TrackMetadata(
        path=Path(path),
        artist=artist,
        album=album,
        album_artist=album_artist,
        date=date,
        mb_album_id=mb_album_id,
    )


# ---------------------------------------------------------------------------
# album_key normalisation
# ---------------------------------------------------------------------------


def test_album_key_uses_album_artist_over_artist() -> None:
    track = _make_track(artist="Track Artist", album_artist="Album Artist")
    key = track.album_key
    assert key[0] == "album artist"


def test_album_key_falls_back_to_artist() -> None:
    track = _make_track(artist="Solo Artist", album_artist=None)
    key = track.album_key
    assert key[0] == "solo artist"


def test_album_key_normalises_case() -> None:
    track = _make_track(artist="ARTIST", album="MY ALBUM", date="2023")
    key = track.album_key
    assert key == ("artist", "my album", "2023")


def test_album_key_truncates_date_to_year() -> None:
    track = _make_track(date="2023-06-15")
    key = track.album_key
    assert key[2] == "2023"


def test_album_key_handles_none_fields() -> None:
    track = _make_track(artist=None, album=None, date=None)
    key = track.album_key
    assert key == ("", "", "")


# ---------------------------------------------------------------------------
# group_by_album
# ---------------------------------------------------------------------------


def test_group_by_album_groups_same_album() -> None:
    tracks = [
        _make_track("t1.flac", artist="A", album="X", date="2020"),
        _make_track("t2.flac", artist="A", album="X", date="2020"),
        _make_track("t3.flac", artist="A", album="Y", date="2020"),
    ]
    groups = group_by_album(tracks)
    assert len(groups) == 2
    x_key = ("a", "x", "2020")
    assert x_key in groups
    assert len(groups[x_key]) == 2


def test_group_by_album_uses_folder_for_missing_album() -> None:
    track = TrackMetadata(path=Path("Artist/MyAlbum/track.flac"), artist="Artist", album=None)
    groups = group_by_album([track])
    # Should use "myalbum" (folder name) as album component
    keys = list(groups.keys())
    assert any("myalbum" in k[1] for k in keys)


def test_group_by_album_empty_input() -> None:
    assert group_by_album([]) == {}


# ---------------------------------------------------------------------------
# display_format
# ---------------------------------------------------------------------------


def test_display_format_flac() -> None:
    track = TrackMetadata(path=Path("t.flac"), format="FLAC", sample_rate=96000, bit_depth=24)
    assert "FLAC" in track.display_format
    assert "96kHz" in track.display_format
    assert "24bit" in track.display_format


def test_display_format_mp3() -> None:
    track = TrackMetadata(path=Path("t.mp3"), format="MP3", bitrate=320)
    assert "MP3" in track.display_format
    assert "320kbps" in track.display_format


# ---------------------------------------------------------------------------
# read_metadata — graceful fallback for missing/corrupt files
# ---------------------------------------------------------------------------


def test_read_metadata_missing_file_returns_partial(tmp_path: Path) -> None:
    fake = tmp_path / "nonexistent.flac"
    result = read_metadata(fake)
    assert result.path == fake
    # Should not raise; other fields default to None
    assert result.title is None
