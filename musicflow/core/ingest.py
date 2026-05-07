"""Ingest pipeline — scan source folders, move/copy files to Staging 1, extract archives.

Rules:
  - Torrent source files are COPIED to staging (originals kept for seeding).
  - Soulseek source files are MOVED to staging (originals deleted).
  - Archive files (ZIP, RAR, 7Z, TAR, TAR.GZ, etc.) are pre-extracted in-place
    in the source folder before scanning (idempotent — re-scanning never double-extracts).
  - Companion files (cover art, booklets, videos, etc.) are carried alongside
    audio files into staging and later into staging-2.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QThread, Signal

from musicflow.core.metadata import read_metadata

if TYPE_CHECKING:
    from musicflow.config import AppConfig
from musicflow.utils.file_utils import (
    ARCHIVE_EXTENSIONS,
    AUDIO_EXTENSIONS,
    _get_archive_extension,
    extract_archive,
    extract_archive_in_place,
    find_companions,
    is_audio_file,
    safe_copy,
    safe_delete,
    safe_move,
    unique_dest,
)
from musicflow.utils.logging_utils import get_logger

logger = get_logger(__name__)


class SourceType(StrEnum):
    TORRENT = "torrent"
    SOULSEEK = "soulseek"


@dataclass
class SourceItem:
    """A file discovered in a source folder before ingestion."""

    path: Path
    source_type: SourceType
    is_archive: bool = False
    is_companion: bool = False  # True for cover art, booklets, videos, etc.
    size: int | None = None  # File size in bytes (cached to avoid .stat() on network shares)

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class StagedItem:
    """A file that has been placed in Staging 1."""

    path: Path           # path inside staging-1
    source_type: SourceType
    original_path: Path  # where it came from
    is_companion: bool = False


@dataclass
class IngestResult:
    staged: list[StagedItem] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        """Number of audio files successfully staged (companions excluded)."""
        return sum(1 for s in self.staged if not s.is_companion)

    @property
    def companion_count(self) -> int:
        """Number of companion files staged."""
        return sum(1 for s in self.staged if s.is_companion)

    @property
    def error_count(self) -> int:
        return len(self.errors)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def scan_source(
    folder: Path,
    source_type: SourceType,
    companion_extensions: list[str] | None = None,
    on_file_found: Callable[[SourceItem], None] | None = None,
    on_archive_progress: Callable[[Path, int, int], None] | None = None,
) -> list[SourceItem]:
    """Recursively scan *folder* and return SourceItems for audio, archives, and companions.

    Archive files (ZIP, RAR, 7Z, TAR, TAR.GZ, etc.) are pre-extracted in-place
    (idempotent) so their audio content is immediately visible as regular SourceItems
    on this and future scans.

    Args:
        folder:               Root source folder to scan.
        source_type:          TORRENT or SOULSEEK.
        companion_extensions: File extensions (e.g. [".jpg", ".nfo"]) to carry
                              alongside audio.  Pass None or [] to skip companion
                              discovery.
        on_file_found:        Optional callback(item) called when each file is discovered.
        on_archive_progress:  Optional callback(archive_path, current, total) called before/after
                              each archive extraction.

    Returns:
        List of SourceItem objects (audio + archives + companions).
    """
    items: list[SourceItem] = []
    archive_paths: list[Path] = []

    if not folder.exists():
        logger.warning("Source folder does not exist: %s", folder)
        return items

    # --- First pass: collect audio files and archives, emit as found ---
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        # Check for archive using helper (handles multi-part extensions like .tar.gz)
        archive_ext = _get_archive_extension(path)
        if archive_ext:
            item = SourceItem(path=path, source_type=source_type, is_archive=True, size=path.stat().st_size)
            items.append(item)
            archive_paths.append(path)
            if on_file_found:
                on_file_found(item)
        elif ext in AUDIO_EXTENSIONS:
            item = SourceItem(path=path, source_type=source_type, size=path.stat().st_size)
            items.append(item)
            if on_file_found:
                on_file_found(item)

    # --- Pre-extract archives in-place (idempotent), emit progress ---
    known_paths: set[Path] = {i.path for i in items}
    for idx, archive_path in enumerate(archive_paths):
        if on_archive_progress:
            on_archive_progress(archive_path, idx, len(archive_paths))
        try:
            extract_dir = extract_archive_in_place(archive_path)
            for p in extract_dir.rglob("*"):
                if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS and p not in known_paths:
                    item = SourceItem(path=p, source_type=source_type, size=p.stat().st_size)
                    items.append(item)
                    known_paths.add(p)
                    if on_file_found:
                        on_file_found(item)
        except Exception as exc:
            logger.warning("Pre-extract failed for %s: %s", archive_path, exc)
    if on_archive_progress and archive_paths:
        on_archive_progress(archive_paths[-1], len(archive_paths), len(archive_paths))

    # --- Discover companion files for each unique audio-file parent folder ---
    if companion_extensions:
        audio_folders: set[Path] = {
            i.path.parent
            for i in items
            if not i.is_archive and not i.is_companion
        }
        for audio_folder in audio_folders:
            for comp_path in find_companions(audio_folder, companion_extensions):
                if comp_path not in known_paths:
                    item = SourceItem(
                        path=comp_path,
                        source_type=source_type,
                        is_companion=True,
                    )
                    items.append(item)
                    known_paths.add(comp_path)
                    if on_file_found:
                        on_file_found(item)

    audio_count = sum(1 for i in items if not i.is_archive and not i.is_companion)
    companion_count = sum(1 for i in items if i.is_companion)
    logger.info(
        "Scanned %s (%s): %d audio, %d archive, %d companion items",
        folder, source_type, audio_count, len(archive_paths), companion_count,
    )
    return items


# ---------------------------------------------------------------------------
# Ingestion helpers
# ---------------------------------------------------------------------------

_SANITISE_RE = re.compile(r'[\\/:*?"<>|]')


def _sanitise_folder(s: str) -> str:
    """Remove invalid folder name characters."""
    return _SANITISE_RE.sub("_", s).strip()


def _album_folder_parts(audio_path: Path, source_root: Path) -> tuple[str, str]:
    """Return (artist_folder, album_folder) derived from tags, with fallbacks.

    Both parts are sanitised for use as folder names.
    Falls back to (source_root.name, top_level_source_subfolder) if tags are missing.
    """
    try:
        meta = read_metadata(audio_path)
        artist = (meta.album_artist or meta.artist or "").strip()
        album = (meta.album or "").strip()
        if artist and album:
            return _sanitise_folder(artist), _sanitise_folder(album)
        if artist:
            # No album tag — use source subfolder as album name
            try:
                rel = audio_path.relative_to(source_root)
                top = rel.parts[0] if len(rel.parts) > 1 else source_root.name
            except ValueError:
                top = source_root.name
            return _sanitise_folder(artist), _sanitise_folder(top)
    except Exception:
        pass
    # Fallback: use source_root.name as artist, top-level subfolder as album
    try:
        rel = audio_path.relative_to(source_root)
        top = rel.parts[0] if len(rel.parts) > 1 else ""
        return _sanitise_folder(source_root.name), _sanitise_folder(top) or _sanitise_folder(source_root.name)
    except ValueError:
        name = _sanitise_folder(source_root.name)
        return name, name


def _build_staged_dest(
    item: SourceItem,
    source_root: Path,
    staging: Path,
) -> Path:
    """Return destination: staging/Artist/Album/source_type/filename."""
    artist_folder, album_folder = _album_folder_parts(item.path, source_root)
    source_subdir = item.source_type.value  # "torrent" or "soulseek"
    return staging / artist_folder / album_folder / source_subdir / item.path.name


def _relative_dest(item: SourceItem, source_root: Path, staging: Path) -> Path:
    """Compute the destination path inside *staging*, preserving relative folder structure."""
    try:
        rel = item.path.relative_to(source_root)
    except ValueError:
        rel = Path(item.path.name)
    
    # Add source type folder (torrent/ or soulseek/)
    source_folder = item.source_type.value
    dest = staging / source_folder / rel
    return unique_dest(dest)


def _ingest_audio(
    item: SourceItem, source_root: Path, staging: Path, result: IngestResult
) -> None:
    base_dest = _build_staged_dest(item, source_root, staging)
    
    # Torrent idempotency: skip if already present
    if item.source_type == SourceType.TORRENT and base_dest.exists():
        result.staged.append(
            StagedItem(path=base_dest, source_type=item.source_type, original_path=item.path)
        )
        return
    
    dest = unique_dest(base_dest)
    match item.source_type:
        case SourceType.TORRENT:
            actual = safe_copy(item.path, dest)
        case SourceType.SOULSEEK:
            actual = safe_move(item.path, dest)
    result.staged.append(
        StagedItem(path=actual, source_type=item.source_type, original_path=item.path)
    )


def _ingest_companion(
    item: SourceItem, source_root: Path, staging: Path, result: IngestResult
) -> None:
    """Copy (Torrent) or move (Soulseek) companion file into the same folder as sibling audio."""
    # Find the staged directory of an audio file from the same source folder
    companion_source_dir = item.path.parent
    sibling_dir: Path | None = None
    for staged in result.staged:
        if not staged.is_companion and staged.original_path.parent == companion_source_dir:
            sibling_dir = staged.path.parent
            break

    if sibling_dir is not None:
        base_dest = sibling_dir / item.path.name
    else:
        # No sibling audio found — fall back to _build_staged_dest
        base_dest = _build_staged_dest(item, source_root, staging)

    dest = unique_dest(base_dest)
    match item.source_type:
        case SourceType.TORRENT:
            actual = safe_copy(item.path, dest)
        case SourceType.SOULSEEK:
            actual = safe_move(item.path, dest)
    result.staged.append(
        StagedItem(
            path=actual,
            source_type=item.source_type,
            original_path=item.path,
            is_companion=True,
        )
    )


def _ingest_archive(
    item: SourceItem,
    source_root: Path,
    staging: Path,
    result: IngestResult,
    companion_extensions: list[str] | None = None,
) -> None:
    """Extract an archive into staging and register its audio (and companion) files.
    
    Supports ZIP, RAR, 7Z, TAR, TAR.GZ, TAR.BZ2, and other formats.
    Raises an exception if extraction fails (will be caught by caller).
    """
    try:
        rel = item.path.relative_to(source_root)
    except ValueError:
        rel = Path(item.path.name)

    # Strip archive extensions (longest first to handle .tar.gz before .gz)
    base_name = rel.name
    name_lower = base_name.lower()
    for ext in sorted(ARCHIVE_EXTENSIONS, key=len, reverse=True):
        if name_lower.endswith(ext):
            base_name = base_name[: -len(ext)]
            break
    
    extract_dir = staging / base_name
    
    # Extract archive — this will raise an exception if it fails
    # which will be caught by ingest_files() and logged appropriately
    extracted = extract_archive(item.path, extract_dir)
    logger.debug("Extracted archive %s: %d files", item.path.name, len(extracted))

    for extracted_path in extracted:
        if is_audio_file(extracted_path):
            result.staged.append(
                StagedItem(
                    path=extracted_path,
                    source_type=item.source_type,
                    original_path=item.path,
                )
            )

    # Register companion files that landed in the extraction dir
    if companion_extensions:
        audio_folders: set[Path] = {
            s.path.parent
            for s in result.staged
            if not s.is_companion and s.original_path == item.path
        }
        registered = {s.path for s in result.staged}
        for audio_folder in audio_folders:
            for comp_path in find_companions(audio_folder, companion_extensions):
                if comp_path.exists() and comp_path not in registered:
                    result.staged.append(
                        StagedItem(
                            path=comp_path,
                            source_type=item.source_type,
                            original_path=item.path,
                            is_companion=True,
                        )
                    )
                    registered.add(comp_path)

    # Delete source archive only for Soulseek
    if item.source_type == SourceType.SOULSEEK:
        safe_delete(item.path)


# ---------------------------------------------------------------------------
# Public ingest entry point
# ---------------------------------------------------------------------------


def ingest_files(
    items: list[SourceItem],
    source_root: Path,
    staging: Path,
    progress_cb: Callable[[int, int, str, bool], None] | None = None,
    companion_extensions: list[str] | None = None,
) -> IngestResult:
    """Move/copy *items* into *staging*.

    Args:
        items:                List of SourceItem to ingest.
        source_root:          Root of the source folder (used to compute relative paths).
        staging:              Destination staging-1 folder.
        progress_cb:          Optional callback(current, total, filename, is_error).
                              is_error=True indicates the file failed to ingest.
        companion_extensions: Extensions to carry alongside audio.

    Returns:
        IngestResult with staged items and any errors.
    """
    result = IngestResult()
    total = len(items)

    for idx, item in enumerate(items):
        if progress_cb:
            progress_cb(idx, total, item.name, False)

        try:
            if item.is_archive:
                _ingest_archive(item, source_root, staging, result, companion_extensions)
            elif item.is_companion:
                _ingest_companion(item, source_root, staging, result)
            else:
                _ingest_audio(item, source_root, staging, result)
        except Exception as exc:
            logger.error("Failed to ingest %s: %s", item.path, exc)
            result.errors.append((item.path, str(exc)))
            # Report error to progress callback
            if progress_cb:
                progress_cb(idx, total, f"{item.name} (ERROR: {str(exc)[:50]})", True)

    if progress_cb:
        progress_cb(total, total, "Done", False)

    logger.info(
        "Ingest complete: %d audio, %d companion, %d errors",
        result.success_count, result.companion_count, result.error_count,
    )
    return result


# ---------------------------------------------------------------------------
# Qt Workers
# ---------------------------------------------------------------------------


def _cleanup_empty_folders(root: Path) -> int:
    """Recursively delete empty folders under root.
    
    Returns number of folders deleted.
    """
    deleted = 0
    # Walk from deepest to shallowest to avoid "directory not empty" errors
    for folder in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if folder.is_dir() and not any(folder.iterdir()):
            try:
                folder.rmdir()
                logger.info("Deleted empty folder: %s", folder)
                deleted += 1
            except OSError as exc:
                logger.warning("Could not delete folder %s: %s", folder, exc)
    return deleted


class ScanWorker(QThread):
    """Background worker that scans source folders and emits progress signals.
    
    Scans Torrent and Soulseek folders in parallel for better performance.
    Emits item_found signal as each file is discovered, and finished signal at the end.
    """

    progress = Signal(int, int, str)                    # current, total, folder_path
    item_found = Signal(object, str)                    # SourceItem, source_type
    archive_progress = Signal(str, int, int, str)       # archive_name, current, total, source_type
    finished = Signal(list, list)                       # torrent_items, soulseek_items
    error = Signal(str)

    def __init__(
        self,
        torrent_path: Path | None,
        soulseek_path: Path | None,
        companion_extensions: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._torrent_path = torrent_path
        self._soulseek_path = soulseek_path
        self._companion_extensions = companion_extensions or []

    def run(self) -> None:
        try:
            torrent_items: list[SourceItem] = []
            soulseek_items: list[SourceItem] = []

            # Scan both folders in parallel using ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {}

                if self._torrent_path:
                    logger.info("Scanning Torrent folder: %s", self._torrent_path)
                    self.progress.emit(0, 2, "Scanning Torrent...")
                    future = executor.submit(
                        scan_source,
                        self._torrent_path,
                        SourceType.TORRENT,
                        self._companion_extensions,
                        lambda item: self.item_found.emit(item, "torrent"),
                        lambda path, cur, tot: self.archive_progress.emit(path.name, cur, tot, "torrent"),
                    )
                    futures[future] = "torrent"

                if self._soulseek_path:
                    logger.info("Scanning Soulseek folder: %s", self._soulseek_path)
                    self.progress.emit(0, 2, "Scanning Soulseek...")
                    future = executor.submit(
                        scan_source,
                        self._soulseek_path,
                        SourceType.SOULSEEK,
                        self._companion_extensions,
                        lambda item: self.item_found.emit(item, "soulseek"),
                        lambda path, cur, tot: self.archive_progress.emit(path.name, cur, tot, "soulseek"),
                    )
                    futures[future] = "soulseek"

                # Collect results as they complete
                for future in as_completed(futures):
                    source_type = futures[future]
                    items = future.result()

                    if source_type == "torrent":
                        torrent_items = items
                        logger.info(
                            "Torrent scan complete: %d items", len(torrent_items)
                        )
                        self.progress.emit(1, 2, "Torrent scan complete")
                    else:
                        soulseek_items = items
                        logger.info(
                            "Soulseek scan complete: %d items", len(soulseek_items)
                        )
                        self.progress.emit(1, 2, "Soulseek scan complete")

            self.progress.emit(2, 2, "Done")
            self.finished.emit(torrent_items, soulseek_items)
        except Exception as exc:
            logger.exception("ScanWorker crashed")
            self.error.emit(str(exc))


class IngestWorker(QThread):
    """Background worker that runs ingest_files and emits progress signals."""

    progress = Signal(int, int, str, bool)   # current, total, filename, is_error
    finished = Signal(object)                # IngestResult
    error = Signal(str)

    def __init__(
        self,
        items: list[SourceItem],
        source_root: Path,
        staging: Path,
        companion_extensions: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._items = items
        self._source_root = source_root
        self._staging = staging
        self._companion_extensions = companion_extensions or []

    def run(self) -> None:
        try:
            result = ingest_files(
                self._items,
                self._source_root,
                self._staging,
                progress_cb=lambda cur, tot, name, is_err: self.progress.emit(cur, tot, name, is_err),
                companion_extensions=self._companion_extensions,
            )
            self.finished.emit(result)
        except Exception as exc:
            logger.exception("IngestWorker crashed")
            self.error.emit(str(exc))


class IngestAnalysisWorker(QThread):
    """Ingest files then run metadata + FFT analysis in parallel."""

    progress = Signal(int, int, str)          # current, total, filename
    file_meta_ready = Signal(object, object)  # StagedItem, TrackMetadata
    file_analysis_ready = Signal(object, object)  # Path, SpectrumResult|None
    finished = Signal()
    error = Signal(str)

    def __init__(
        self,
        items: list[SourceItem],
        source_root: Path,
        staging: Path,
        config: "AppConfig",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._items = items
        self._source_root = source_root
        self._staging = staging
        self._config = config

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            logger.exception("IngestAnalysisWorker crashed")
            self.error.emit(str(exc))

    def _run(self) -> None:
        from musicflow.core.fake_hires import analyze_file, save_spectrum_npz, load_spectrum_npz

        audio_items = [i for i in self._items if not i.is_archive and not i.is_companion]
        total = len(audio_items)

        # Stage all files first
        staged_items: list[StagedItem] = []
        for idx, item in enumerate(audio_items):
            self.progress.emit(idx, total, item.path.name)
            base_dest = _build_staged_dest(item, self._source_root, self._staging)
            if item.source_type == SourceType.TORRENT and base_dest.exists():
                staged_items.append(StagedItem(
                    path=base_dest,
                    source_type=item.source_type,
                    original_path=item.path,
                    is_companion=False,
                ))
                continue
            dest = unique_dest(base_dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if item.source_type == SourceType.TORRENT:
                safe_copy(item.path, dest)
            else:
                safe_move(item.path, dest)
            staged_items.append(StagedItem(
                path=dest,
                source_type=item.source_type,
                original_path=item.path,
                is_companion=False,
            ))

        # Emit metadata first so the UI can show rows immediately.
        for si in staged_items:
            try:
                meta = read_metadata(si.path)
                self.file_meta_ready.emit(si, meta)
            except Exception as exc:
                logger.error("Metadata failed for %s: %s", si.path, exc)

        # Run analysis in parallel
        cfg = self._config
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(self._analyze_file, si, cfg): si
                for si in staged_items
            }
            for future in as_completed(futures):
                si = futures[future]
                try:
                    spectrum = future.result()
                    self.file_analysis_ready.emit(si.path, spectrum)
                except Exception as exc:
                    logger.error("Analysis failed for %s: %s", si.path, exc)
                    self.file_analysis_ready.emit(si.path, None)

        self.finished.emit()

    def _analyze_file(self, si: StagedItem, cfg):
        """Thread-safe analysis."""
        from musicflow.core.fake_hires import analyze_file, save_spectrum_npz, load_spectrum_npz

        spectrum = load_spectrum_npz(si.path)
        if spectrum is None:
            try:
                spectrum = analyze_file(
                    si.path, cfg.fake_hires_threshold,
                    cfg.fake_hires_db_floor, cfg.fake_hires_analysis_seconds,
                )
                save_spectrum_npz(spectrum, si.path)
            except Exception:
                spectrum = None
        return spectrum


class _MultiSourceIngestAnalysisWorker(QThread):
    """Ingest Torrent + Soulseek items then run metadata + FFT analysis in parallel.

    Emits file_ready for each audio file as it finishes analysis.
    Emits finished(IngestResult) when all files are done.
    """

    progress = Signal(int, int, str)              # current, total, filename
    file_meta_ready = Signal(object, object)      # StagedItem, TrackMetadata
    file_analysis_ready = Signal(object, object)  # Path, SpectrumResult|None
    finished = Signal(object)                     # IngestResult
    error = Signal(str)

    def __init__(
        self,
        torrent_items: list[SourceItem],
        soulseek_items: list[SourceItem],
        torrent_root: Path,
        soulseek_root: Path,
        staging: Path,
        config: "AppConfig",
        companion_extensions: list[str] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._torrent_items = torrent_items
        self._soulseek_items = soulseek_items
        self._torrent_root = torrent_root
        self._soulseek_root = soulseek_root
        self._staging = staging
        self._config = config
        self._companion_extensions = companion_extensions or []

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            logger.exception("_MultiSourceIngestAnalysisWorker crashed")
            self.error.emit(str(exc))

    def _run(self) -> None:
        from musicflow.core.fake_hires import analyze_file, load_spectrum_npz, save_spectrum_npz

        result = IngestResult()

        # Stage all files: Torrent items then Soulseek items, in list order.
        # _ingest_companion relies on audio being staged first (scan_source guarantees
        # companions appear after audio in the list).
        all_items: list[tuple[SourceItem, Path]] = [
            (item, self._torrent_root) for item in self._torrent_items
        ] + [
            (item, self._soulseek_root) for item in self._soulseek_items
        ]
        total_stage = len(all_items)

        for idx, (item, source_root) in enumerate(all_items):
            self.progress.emit(idx, total_stage, item.path.name)
            try:
                if item.is_archive:
                    _ingest_archive(
                        item, source_root, self._staging, result,
                        companion_extensions=self._companion_extensions,
                    )
                elif item.is_companion:
                    _ingest_companion(item, source_root, self._staging, result)
                else:
                    _ingest_audio(item, source_root, self._staging, result)
            except Exception as exc:
                logger.error("Failed to stage %s: %s", item.path, exc)
                result.errors.append((item.path, str(exc)))

        # Cleanup empty Soulseek source folders after all moves
        if self._soulseek_items:
            deleted = _cleanup_empty_folders(self._soulseek_root)
            if deleted:
                logger.info("Cleaned up %d empty folders in Soulseek source", deleted)

        # Emit metadata first so the UI can render rows before analysis finishes.
        for si in audio_staged:
            try:
                meta = read_metadata(si.path)
                self.file_meta_ready.emit(si, meta)
            except Exception as exc:
                logger.error("Metadata failed for %s: %s", si.path, exc)

        # Run FFT analysis in parallel on all staged audio files
        audio_staged = [s for s in result.staged if not s.is_companion]
        total_analyze = len(audio_staged)
        cfg = self._config

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(self._analyze_file, si, cfg): si
                for si in audio_staged
            }
            done = 0
            for future in as_completed(futures):
                si = futures[future]
                done += 1
                self.progress.emit(
                    total_stage + done,
                    total_stage + total_analyze,
                    si.path.name,
                )
                try:
                    spectrum = future.result()
                    self.file_analysis_ready.emit(si.path, spectrum)
                except Exception as exc:
                    logger.error("Analysis failed for %s: %s", si.path, exc)
                    self.file_analysis_ready.emit(si.path, None)

        self.finished.emit(result)

    def _analyze_file(self, si: StagedItem, cfg: "AppConfig"):
        """Thread-safe: read metadata + load or compute spectrum."""
        from musicflow.core.fake_hires import analyze_file, load_spectrum_npz, save_spectrum_npz

        spectrum = load_spectrum_npz(si.path)
        if spectrum is None:
            try:
                spectrum = analyze_file(
                    si.path,
                    cfg.fake_hires_threshold,
                    cfg.fake_hires_db_floor,
                    cfg.fake_hires_analysis_seconds,
                )
                save_spectrum_npz(spectrum, si.path)
            except Exception:
                spectrum = None
        return spectrum


def _infer_source_type(path: Path) -> SourceType:
    """Infer the source type from a staged file path."""
    if path.parent.name.lower() == "soulseek":
        return SourceType.SOULSEEK
    return SourceType.TORRENT


def _staged_audio_paths(root: Path) -> list[Path]:
    """Return staged audio paths under *root*."""
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS]


class LoadFromStagingWorker(QThread):
    """Load an existing Staging 1 tree without ingesting source folders again."""

    file_meta_ready = Signal(object, object)
    file_analysis_ready = Signal(object, object)
    progress = Signal(int, int, str)
    finished = Signal()
    error = Signal(str)

    def __init__(self, staging: Path, config: "AppConfig", parent=None) -> None:
        super().__init__(parent)
        self._staging = staging
        self._config = config
        self._aborted_paths: set[Path] = set()

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            logger.exception("LoadFromStagingWorker crashed")
            self.error.emit(str(exc))

    def _run(self) -> None:
        paths = _staged_audio_paths(self._staging)
        total = len(paths)
        cfg = self._config

        for idx, path in enumerate(paths, start=1):
            staged = StagedItem(
                path=path,
                source_type=_infer_source_type(path),
                original_path=path,
                is_companion=False,
            )
            meta = read_metadata(path)
            self.progress.emit(idx, total, path.name)
            self.file_meta_ready.emit(staged, meta)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self._analyze_file, path, cfg): path for path in paths}
            done = 0
            for future in as_completed(futures):
                path = futures[future]
                done += 1
                self.progress.emit(done, total, path.name)
                try:
                    spectrum = future.result()
                    self.file_analysis_ready.emit(path, spectrum)
                except Exception as exc:
                    logger.error("Failed to load %s: %s", path, exc)
                    self.file_analysis_ready.emit(path, None)

        self.finished.emit()

    def abort_paths(self, paths: list[Path]) -> None:
        self._aborted_paths.update(paths)

    def _analyze_file(self, path: Path, cfg: "AppConfig"):
        from musicflow.core.fake_hires import analyze_file, load_spectrum_npz, save_spectrum_npz

        spectrum = load_spectrum_npz(path)
        if path in self._aborted_paths:
            return None
        if spectrum is None and path.suffix.lower() in AUDIO_EXTENSIONS:
            try:
                spectrum = analyze_file(
                    path,
                    cfg.fake_hires_threshold,
                    cfg.fake_hires_db_floor,
                    cfg.fake_hires_analysis_seconds,
                )
                save_spectrum_npz(spectrum, path)
            except Exception:
                spectrum = None
        return spectrum


class RescanWorker(QThread):
    """Re-read metadata for staged audio files, optionally using MusicBrainz."""

    file_rescanned = Signal(object, object, object)
    progress = Signal(int, int, str)
    finished = Signal()
    error = Signal(str)

    def __init__(self, paths: list[Path], config: "AppConfig", use_musicbrainz: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._paths = paths
        self._config = config
        self._use_musicbrainz = use_musicbrainz

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            logger.exception("RescanWorker crashed")
            self.error.emit(str(exc))

    def _run(self) -> None:
        from musicflow.core.fake_hires import load_spectrum_npz
        from musicflow.core.musicbrainz import lookup_album, setup

        if self._use_musicbrainz:
            setup(self._config.musicbrainz_user_agent)

        total = len(self._paths)
        for idx, path in enumerate(self._paths, start=1):
            self.progress.emit(idx, total, path.name)
            try:
                meta = read_metadata(path)
                if self._use_musicbrainz:
                    mb_info = lookup_album(meta.album_key)
                    if mb_info is not None:
                        from dataclasses import replace

                        meta = replace(
                            meta,
                            album=mb_info.title or meta.album,
                            album_artist=mb_info.artist or meta.album_artist,
                            date=mb_info.date or meta.date,
                            mb_album_id=mb_info.mb_release_id,
                        )
                target_path = self._target_path(path, meta)
                if target_path != path:
                    path = safe_move(path, target_path)
                # Use the final on-disk path so the library can update in place.
                spectrum = load_spectrum_npz(path)
                staged = StagedItem(
                    path=path,
                    source_type=_infer_source_type(path),
                    original_path=path,
                    is_companion=False,
                )
                self.file_rescanned.emit(staged, meta, spectrum)
            except Exception as exc:
                logger.error("Rescan failed for %s: %s", path, exc)

        self.finished.emit()

    def _target_path(self, path: Path, meta: "TrackMetadata") -> Path:
        album = (meta.album or "Unknown Album").strip()
        artist = (meta.album_artist or meta.artist or "Unknown Artist").strip()
        year = (meta.date or "")[:4]
        album_label = f"{album} ({year})" if year else album
        source_dir = path.parent.name
        return path.parent.parent.parent / _sanitise_folder(artist) / _sanitise_folder(album_label) / source_dir / path.name


class ReanalyseWorker(QThread):
    """Re-run hi-res analysis for staged audio files."""

    file_reanalysed = Signal(object, object, object)
    progress = Signal(int, int, str)
    finished = Signal()
    error = Signal(str)

    def __init__(self, paths: list[Path], config: "AppConfig", parent=None) -> None:
        super().__init__(parent)
        self._paths = paths
        self._config = config

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            logger.exception("ReanalyseWorker crashed")
            self.error.emit(str(exc))

    def _run(self) -> None:
        from musicflow.core.fake_hires import analyze_file, load_spectrum_npz, save_spectrum_npz

        total = len(self._paths)
        cfg = self._config
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self._reanalyse_file, path, cfg): path for path in self._paths}
            done = 0
            for future in as_completed(futures):
                path = futures[future]
                done += 1
                self.progress.emit(done, total, path.name)
                try:
                    staged, meta, spectrum = future.result()
                    self.file_reanalysed.emit(staged, meta, spectrum)
                except Exception as exc:
                    logger.error("Re-analysis failed for %s: %s", path, exc)

        self.finished.emit()

    def _reanalyse_file(self, path: Path, cfg: "AppConfig") -> tuple[StagedItem, TrackMetadata, object | None]:
        from musicflow.core.fake_hires import analyze_file, save_spectrum_npz
        from musicflow.core.fake_hires import load_spectrum_npz

        npz_path = path.with_name(f"{path.stem}.spectrum.npz")
        if npz_path.exists():
            npz_path.unlink()
        meta = read_metadata(path)
        try:
            spectrum = analyze_file(
                path,
                cfg.fake_hires_threshold,
                cfg.fake_hires_db_floor,
                cfg.fake_hires_analysis_seconds,
            )
            save_spectrum_npz(spectrum, path)
        except Exception:
            spectrum = None
        staged = StagedItem(
            path=path,
            source_type=_infer_source_type(path),
            original_path=path,
            is_companion=False,
        )
        return staged, meta, spectrum
