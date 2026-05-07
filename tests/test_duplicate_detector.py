"""Tests for musicflow.core.duplicate_detector."""

from __future__ import annotations

from pathlib import Path

import pytest

from musicflow.core.duplicate_detector import (
    AlbumInstance,
    DuplicateGroup,
    DuplicateReason,
    build_album_instances,
    detect_duplicates,
)
from musicflow.core.metadata import AlbumKey, TrackMetadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _track(path: str, artist: str = "Artist", album: str = "Album", date: str = "2023") -> TrackMetadata:
    return TrackMetadata(
        path=Path(path),
        artist=artist,
        album=album,
        date=date,
        format="FLAC",
        sample_rate=44100,
        bit_depth=16,
    )


def _instance(
    key: AlbumKey,
    folder: str,
    tracks: list[TrackMetadata],
    mb_release_id: str | None = None,
) -> AlbumInstance:
    from musicflow.core.musicbrainz import MBAlbumInfo

    mb_info = None
    if mb_release_id:
        mb_info = MBAlbumInfo(
            mb_release_id=mb_release_id,
            title="",
            artist="",
            date="",
            label=None,
            track_count=len(tracks),
            format=None,
            country=None,
        )
    return AlbumInstance(key=key, tracks=tracks, folder=Path(folder), mb_info=mb_info)


# ---------------------------------------------------------------------------
# build_album_instances
# ---------------------------------------------------------------------------


def test_build_instances_splits_by_folder() -> None:
    key: AlbumKey = ("artist", "album", "2023")
    tracks = [
        _track("folder_a/t1.flac"),
        _track("folder_b/t2.flac"),
    ]
    # Manually set parents
    tracks[0] = TrackMetadata(path=Path("folder_a/t1.flac"), artist="Artist", album="Album", date="2023")
    tracks[1] = TrackMetadata(path=Path("folder_b/t2.flac"), artist="Artist", album="Album", date="2023")

    groups = {key: tracks}
    instances = build_album_instances(groups)
    # Two different parent folders → two instances
    assert len(instances) == 2


# ---------------------------------------------------------------------------
# detect_duplicates — same MB ID
# ---------------------------------------------------------------------------


def test_same_mb_id_is_definite_duplicate() -> None:
    key: AlbumKey = ("artist", "album", "2023")
    t1 = _track("a/t1.flac")
    t2 = _track("b/t2.flac")
    inst_a = _instance(key, "folder_a", [t1], mb_release_id="abc-123")
    inst_b = _instance(key, "folder_b", [t2], mb_release_id="abc-123")

    groups = detect_duplicates([inst_a, inst_b])
    assert len(groups) == 1
    assert groups[0].reason == DuplicateReason.SAME_MB_ID
    assert groups[0].confidence == 1.0


# ---------------------------------------------------------------------------
# detect_duplicates — same album different folder
# ---------------------------------------------------------------------------


def test_same_album_different_folder_is_probable_duplicate() -> None:
    key: AlbumKey = ("artist", "album", "2023")
    t1 = _track("a/t1.flac")
    t2 = _track("b/t2.flac")
    inst_a = _instance(key, "folder_a", [t1])
    inst_b = _instance(key, "folder_b", [t2])

    groups = detect_duplicates([inst_a, inst_b])
    assert len(groups) == 1
    assert groups[0].reason == DuplicateReason.SAME_ALBUM_DIFFERENT_FOLDER
    assert groups[0].confidence >= 0.85


# ---------------------------------------------------------------------------
# detect_duplicates — different albums → no duplicates
# ---------------------------------------------------------------------------


def test_different_albums_not_duplicates() -> None:
    key_a: AlbumKey = ("artist", "album_a", "2023")
    key_b: AlbumKey = ("artist", "album_b", "2023")
    inst_a = _instance(key_a, "folder_a", [_track("a/t1.flac")])
    inst_b = _instance(key_b, "folder_b", [_track("b/t2.flac")])

    groups = detect_duplicates([inst_a, inst_b])
    assert len(groups) == 0


# ---------------------------------------------------------------------------
# detect_duplicates — empty input
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty() -> None:
    assert detect_duplicates([]) == []


# ---------------------------------------------------------------------------
# AlbumInstance properties
# ---------------------------------------------------------------------------


def test_album_instance_display_quality() -> None:
    key: AlbumKey = ("artist", "album", "2023")
    tracks = [
        TrackMetadata(path=Path("t.flac"), format="FLAC", sample_rate=96000, bit_depth=24),
    ]
    inst = AlbumInstance(key=key, tracks=tracks, folder=Path("folder"))
    assert "FLAC" in inst.display_quality
    assert "96000" in inst.display_quality


def test_duplicate_group_display_label() -> None:
    key: AlbumKey = ("pink floyd", "the wall", "1979")
    inst = _instance(key, "folder", [_track("t.flac")])
    group = DuplicateGroup(
        instances=[inst, inst],
        reason=DuplicateReason.SAME_MB_ID,
        confidence=1.0,
    )
    label = group.display_label
    assert "pink floyd" in label.lower()
    assert "the wall" in label.lower()
    assert "1979" in label
