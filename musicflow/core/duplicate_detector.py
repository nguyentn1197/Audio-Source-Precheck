"""Album-based duplicate detection for MusicFlow.

A 'duplicate' means the same album exists in multiple versions/qualities.
Detection runs in three tiers:
  1. Same MusicBrainz release ID → definite duplicate (confidence 1.0).
  2. Same normalised artist + album, different folder → probable duplicate (0.85).
  3. Same artist + album, different format/quality → quality variant (0.70).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from musicflow.core.metadata import AlbumKey, TrackMetadata, group_by_album
from musicflow.core.musicbrainz import MBAlbumInfo
from musicflow.utils.logging_utils import get_logger

logger = get_logger(__name__)


class DuplicateReason(StrEnum):
    SAME_MB_ID = "Same MusicBrainz release ID"
    SAME_ALBUM_DIFFERENT_FOLDER = "Same album in different folders"
    QUALITY_VARIANT = "Same album, different quality/format"


@dataclass
class AlbumInstance:
    """One version of an album found in staging-1."""

    key: AlbumKey
    tracks: list[TrackMetadata]
    folder: Path  # common parent folder of the tracks
    mb_info: MBAlbumInfo | None = None

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    @property
    def formats(self) -> set[str]:
        return {t.format for t in self.tracks if t.format}

    @property
    def sample_rates(self) -> set[int]:
        return {t.sample_rate for t in self.tracks if t.sample_rate}

    @property
    def bit_depths(self) -> set[int]:
        return {t.bit_depth for t in self.tracks if t.bit_depth}

    @property
    def display_quality(self) -> str:
        fmts = "/".join(sorted(self.formats)) or "?"
        rates = "/".join(str(r) for r in sorted(self.sample_rates)) or "?"
        depths = "/".join(str(d) for d in sorted(self.bit_depths))
        if depths:
            return f"{fmts} {rates}Hz {depths}bit"
        return fmts

    @property
    def mb_release_id(self) -> str | None:
        if self.mb_info:
            return self.mb_info.mb_release_id
        # Check if any track has an MB album ID tag
        for t in self.tracks:
            if t.mb_album_id:
                return t.mb_album_id
        return None


@dataclass
class DuplicateGroup:
    """Two or more AlbumInstances that appear to be the same release."""

    instances: list[AlbumInstance]
    reason: DuplicateReason
    confidence: float  # 0.0–1.0

    @property
    def display_label(self) -> str:
        first = self.instances[0]
        artist, album, year = first.key
        label = f"{artist} — {album}"
        if year:
            label += f" ({year})"
        return label


# ---------------------------------------------------------------------------
# Core detection logic
# ---------------------------------------------------------------------------


def _common_folder(tracks: list[TrackMetadata]) -> Path:
    """Return the common parent directory of all track paths."""
    if not tracks:
        return Path(".")
    folders = [t.path.parent for t in tracks]
    if len(set(folders)) == 1:
        return folders[0]
    # Walk up to find common ancestor
    common = folders[0]
    for folder in folders[1:]:
        while common not in folder.parents and common != folder:
            common = common.parent
    return common


def build_album_instances(
    groups: dict[AlbumKey, list[TrackMetadata]],
    mb_results: dict[AlbumKey, MBAlbumInfo | None] | None = None,
) -> list[AlbumInstance]:
    """Convert grouped tracks into AlbumInstance objects."""
    instances: list[AlbumInstance] = []
    for key, tracks in groups.items():
        # Split into sub-instances by folder (tracks from different folders = different copies)
        by_folder: dict[Path, list[TrackMetadata]] = {}
        for track in tracks:
            folder = track.path.parent
            by_folder.setdefault(folder, []).append(track)

        for folder, folder_tracks in by_folder.items():
            mb_info = (mb_results or {}).get(key)
            instances.append(
                AlbumInstance(
                    key=key,
                    tracks=folder_tracks,
                    folder=folder,
                    mb_info=mb_info,
                )
            )
    return instances


def detect_duplicates(
    instances: list[AlbumInstance],
) -> list[DuplicateGroup]:
    """Detect duplicate album instances and return grouped results.

    Args:
        instances: All AlbumInstances from staging-1.

    Returns:
        List of DuplicateGroups (only groups with 2+ instances).
    """
    groups: list[DuplicateGroup] = []
    used: set[int] = set()

    for i, inst_a in enumerate(instances):
        if i in used:
            continue
        duplicates = [inst_a]
        best_reason = DuplicateReason.QUALITY_VARIANT
        best_confidence = 0.0

        for j, inst_b in enumerate(instances):
            if j <= i or j in used:
                continue
            reason, confidence = _compare(inst_a, inst_b)
            if confidence >= 0.70:
                duplicates.append(inst_b)
                used.add(j)
                if confidence > best_confidence:
                    best_confidence = confidence
                    best_reason = reason

        if len(duplicates) > 1:
            used.add(i)
            groups.append(
                DuplicateGroup(
                    instances=duplicates,
                    reason=best_reason,
                    confidence=best_confidence,
                )
            )

    logger.info("Duplicate detection: %d groups found from %d instances", len(groups), len(instances))
    return groups


def _compare(a: AlbumInstance, b: AlbumInstance) -> tuple[DuplicateReason, float]:
    """Return (reason, confidence) for a pair of AlbumInstances."""
    # Tier 1: same MB release ID
    if a.mb_release_id and b.mb_release_id and a.mb_release_id == b.mb_release_id:
        return DuplicateReason.SAME_MB_ID, 1.0

    # Normalised keys must share artist + album (year can differ)
    a_artist, a_album, _ = a.key
    b_artist, b_album, _ = b.key
    if not a_album or not b_album:
        return DuplicateReason.QUALITY_VARIANT, 0.0
    if a_artist != b_artist or a_album != b_album:
        return DuplicateReason.QUALITY_VARIANT, 0.0

    # Tier 2: same key, different folder
    if a.folder != b.folder:
        return DuplicateReason.SAME_ALBUM_DIFFERENT_FOLDER, 0.85

    # Tier 3: same key, same folder but different format (shouldn't happen often)
    if a.formats != b.formats or a.sample_rates != b.sample_rates:
        return DuplicateReason.QUALITY_VARIANT, 0.70

    return DuplicateReason.QUALITY_VARIANT, 0.0
