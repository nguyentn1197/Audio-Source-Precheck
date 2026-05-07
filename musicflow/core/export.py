"""Export selected files from Staging 1 to Staging 2.

Files are moved (not copied) to staging-2, preserving a clean Artist/Album
folder structure for MusicBrainz Picard to process.

Companion files (cover art, booklets, videos, etc.) in the same album folder
are moved alongside the audio tracks.  If a companion was already moved by a
previous track in the same album, the missing-file case is silently skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from musicflow.core.metadata import TrackMetadata
from musicflow.utils.file_utils import find_companions, safe_move, unique_dest
from musicflow.utils.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class ExportResult:
    moved: list[tuple[Path, Path]] = field(default_factory=list)   # (src, dst)
    errors: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return len(self.moved)

    @property
    def error_count(self) -> int:
        return len(self.errors)


def _sanitise(s: str) -> str:
    for ch in r'\/:*?"<>|':
        s = s.replace(ch, "_")
    return s.strip(". ")


def _build_dest(track: TrackMetadata, staging2: Path) -> Path:
    """Compute destination path inside staging-2.

    Structure: staging2 / <album_artist or artist> / <album> / <filename>
    Falls back to "Unknown Artist" / "Unknown Album" if metadata is missing.
    """
    artist = _sanitise((track.album_artist or track.artist or "Unknown Artist").strip())
    album = _sanitise((track.album or "Unknown Album").strip())
    dest_dir = staging2 / artist / album
    return unique_dest(dest_dir / track.path.name)


def export_to_staging2(
    tracks: list[TrackMetadata],
    staging2: Path,
    companion_extensions: list[str] | None = None,
) -> ExportResult:
    """Move *tracks* from Staging 1 into *staging2*.

    Companion files (cover art, booklets, etc.) found in the same folder as
    each audio track are also moved to the corresponding destination folder.
    If a companion has already been moved by a previous track in the same
    album, it is silently skipped.

    Args:
        tracks:               TrackMetadata objects for files to export.
        staging2:             Destination staging-2 folder.
        companion_extensions: Extensions to recognise as companion files.

    Returns:
        ExportResult with moved files and any errors.
    """
    result = ExportResult()
    staging2.mkdir(parents=True, exist_ok=True)

    # Track which companion source paths have already been moved this run
    moved_companions: set[Path] = set()

    for track in tracks:
        src = track.path
        if not src.exists():
            result.errors.append((src, "File not found"))
            logger.warning("Export skipped (not found): %s", src)
            continue
        try:
            dst = _build_dest(track, staging2)
            actual_dst = safe_move(src, dst)
            result.moved.append((src, actual_dst))
            logger.info("Exported %s -> %s", src.name, actual_dst)

            # Move companion files from the same source folder
            if companion_extensions:
                dest_dir = actual_dst.parent
                for comp in find_companions(src.parent, companion_extensions):
                    if comp in moved_companions or not comp.exists():
                        continue
                    try:
                        comp_dst = unique_dest(dest_dir / comp.name)
                        safe_move(comp, comp_dst)
                        moved_companions.add(comp)
                        logger.debug("Exported companion %s -> %s", comp.name, comp_dst)
                    except Exception as exc:
                        logger.warning("Could not move companion %s: %s", comp, exc)

        except Exception as exc:
            logger.error("Export failed for %s: %s", src, exc)
            result.errors.append((src, str(exc)))

    logger.info(
        "Export complete: %d moved, %d errors", result.success_count, result.error_count
    )
    return result
