"""Safe file operation wrappers for MusicFlow.

All destructive file operations (move, delete) in the application must go through
these helpers.  Business logic must NOT call os.remove / shutil.move directly.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

try:
    import py7zr
except ImportError:
    py7zr = None  # type: ignore

try:
    import rarfile
except ImportError:
    rarfile = None  # type: ignore

from musicflow.utils.logging_utils import get_logger

logger = get_logger(__name__)

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".flac", ".mp3", ".aac", ".m4a", ".wav", ".aiff", ".ogg", ".opus"}
)

ARCHIVE_EXTENSIONS: frozenset[str] = frozenset(
    {".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2"}
)


def safe_copy(src: Path, dst: Path) -> Path:
    """Copy *src* to *dst*, creating parent directories as needed.

    Returns the destination path.  Raises OSError on failure.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = shutil.copy2(src, dst)
    logger.debug("Copied %s -> %s", src, dst)
    return Path(result)


def safe_move(src: Path, dst: Path) -> Path:
    """Move *src* to *dst*, creating parent directories as needed.

    Returns the destination path.  Raises OSError on failure.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = shutil.move(str(src), dst)
    logger.debug("Moved %s -> %s", src, dst)
    return Path(result)


def safe_delete(path: Path) -> None:
    """Delete a file.  Logs a warning if the file does not exist."""
    if not path.exists():
        logger.warning("safe_delete: path does not exist: %s", path)
        return
    path.unlink()
    logger.debug("Deleted %s", path)


def _get_archive_extension(path: Path) -> str | None:
    """Return the archive extension for *path*, or None if not an archive.

    Handles multi-part extensions like .tar.gz, .tar.bz2.
    """
    name_lower = path.name.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tgz", ".tbz2"):
        if name_lower.endswith(ext):
            return ext
    return path.suffix.lower() if path.suffix.lower() in ARCHIVE_EXTENSIONS else None


def _get_archive_base_name(path: Path) -> str:
    """Return the base name of an archive, stripping all archive extensions.
    
    For example:
      - Album.zip → Album
      - Album.tar.gz → Album
      - Album.tar.bz2 → Album
      - Album.7z → Album
    """
    name = path.name
    ext = _get_archive_extension(path)
    if ext:
        return name[: -len(ext)]
    return name


def extract_archive(
    archive_path: Path, dest_dir: Path, max_depth: int = 3, _depth: int = 0
) -> list[Path]:
    """Extract *archive_path* into *dest_dir*.

    Supports: ZIP, RAR, 7Z, TAR, TAR.GZ, TAR.BZ2.
    Nested archives are extracted recursively up to *max_depth* levels.
    Returns a list of all extracted file paths (non-archive files only).

    Raises:
        ValueError: if the archive format is not supported.
        Exception: if extraction fails.
    """
    if _depth > max_depth:
        logger.warning("Max archive extraction depth (%d) reached at %s", max_depth, archive_path)
        return []

    extracted: list[Path] = []
    dest_dir.mkdir(parents=True, exist_ok=True)

    ext = _get_archive_extension(archive_path)
    if not ext:
        raise ValueError(f"Unknown archive format: {archive_path}")

    # --- ZIP ---
    if ext == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest_dir)
        # Iterate over extracted files using rglob to avoid path construction issues
        for extracted_path in dest_dir.rglob("*"):
            if extracted_path.is_file():
                nested_ext = _get_archive_extension(extracted_path)
                if nested_ext:
                    nested_dir = extracted_path.with_suffix("")
                    try:
                        nested_files = extract_archive(
                            extracted_path, nested_dir, max_depth=max_depth, _depth=_depth + 1
                        )
                        extracted.extend(nested_files)
                        safe_delete(extracted_path)
                    except Exception as exc:
                        logger.warning("Nested extraction failed for %s: %s", extracted_path, exc)
                        extracted.append(extracted_path)
                else:
                    extracted.append(extracted_path)

    # --- RAR ---
    elif ext == ".rar":
        if rarfile is None:
            raise ValueError("rarfile module not installed; install with: pip install rarfile")
        try:
            # Extract RAR to temp directory first to avoid Windows path length issues (260 char limit)
            # This is necessary because some RAR files have deeply nested structures
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                with rarfile.RarFile(archive_path, "r") as rf:
                    rf.extractall(temp_path)
                # Move extracted files to destination
                for extracted_path in temp_path.rglob("*"):
                    if extracted_path.is_file():
                        rel_path = extracted_path.relative_to(temp_path)
                        dest_file = dest_dir / rel_path
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(extracted_path), str(dest_file))
                        
                        nested_ext = _get_archive_extension(dest_file)
                        if nested_ext:
                            nested_dir = dest_file.with_suffix("")
                            try:
                                nested_files = extract_archive(
                                    dest_file, nested_dir, max_depth=max_depth, _depth=_depth + 1
                                )
                                extracted.extend(nested_files)
                                safe_delete(dest_file)
                            except Exception as exc:
                                logger.warning("Nested extraction failed for %s: %s", dest_file, exc)
                                extracted.append(dest_file)
                        else:
                            extracted.append(dest_file)
        except Exception as exc:
            logger.error("RAR extraction failed for %s: %s", archive_path, exc)
            raise

    # --- 7Z ---
    elif ext == ".7z":
        if py7zr is None:
            raise ValueError("py7zr module not installed; install with: pip install py7zr")
        with py7zr.SevenZipFile(archive_path, "r") as zf:
            zf.extractall(dest_dir)
            for item_path in dest_dir.rglob("*"):
                if item_path.is_file():
                    nested_ext = _get_archive_extension(item_path)
                    if nested_ext:
                        nested_dir = item_path.with_suffix("")
                        try:
                            nested_files = extract_archive(
                                item_path, nested_dir, max_depth=max_depth, _depth=_depth + 1
                            )
                            extracted.extend(nested_files)
                            safe_delete(item_path)
                        except Exception as exc:
                            logger.warning("Nested extraction failed for %s: %s", item_path, exc)
                            extracted.append(item_path)
                    else:
                        extracted.append(item_path)

    # --- TAR variants ---
    elif ext in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2"):
        # Determine mode based on extension
        if ext in (".tar.gz", ".tgz"):
            mode = "r:gz"
        elif ext in (".tar.bz2", ".tbz2"):
            mode = "r:bz2"
        else:
            mode = "r"

        with tarfile.open(archive_path, mode) as tf:
            tf.extractall(dest_dir)
            for member in tf.getmembers():
                extracted_path = dest_dir / member.name
                if extracted_path.is_file():
                    nested_ext = _get_archive_extension(extracted_path)
                    if nested_ext:
                        nested_dir = extracted_path.with_suffix("")
                        try:
                            nested_files = extract_archive(
                                extracted_path, nested_dir, max_depth=max_depth, _depth=_depth + 1
                            )
                            extracted.extend(nested_files)
                            safe_delete(extracted_path)
                        except Exception as exc:
                            logger.warning("Nested extraction failed for %s: %s", extracted_path, exc)
                            extracted.append(extracted_path)
                    else:
                        extracted.append(extracted_path)

    else:
        raise ValueError(f"Unsupported archive format: {ext}")

    logger.info("Extracted archive %s -> %s (%d entries)", archive_path, dest_dir, len(extracted))
    return extracted


def is_audio_file(path: Path) -> bool:
    """Return True if *path* has a recognised audio extension."""
    return path.suffix.lower() in AUDIO_EXTENSIONS


def unique_dest(dst: Path) -> Path:
    """Return *dst* if it does not exist, otherwise append -1, -2, … until unique."""
    if not dst.exists():
        return dst
    stem = dst.stem
    suffix = dst.suffix
    counter = 1
    while True:
        candidate = dst.with_name(f"{stem}-{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Explorer integration
# ---------------------------------------------------------------------------


def open_in_explorer(path: Path) -> None:
    """Open Windows File Explorer at *path*.

    If *path* is a file, Explorer opens its parent folder with the file
    selected.  If *path* is a folder, Explorer opens that folder directly.
    Non-existent paths open the nearest existing ancestor.
    """
    if platform.system() != "Windows":
        logger.warning("open_in_explorer is only supported on Windows")
        return
    try:
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            # Use the path itself, or walk up to the first existing ancestor
            target = path
            while not target.exists() and target != target.parent:
                target = target.parent
            subprocess.Popen(["explorer", str(target)])
    except OSError as exc:
        logger.warning("open_in_explorer failed for %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------


def is_archive_already_extracted(archive_path: Path) -> bool:
    """Return True if a sibling folder named after the archive base exists and
    contains at least one audio file (at any depth).

    This is the idempotency guard used by extract_archive_in_place().
    Works for all archive types (ZIP, RAR, 7Z, TAR, TAR.GZ, etc.).
    """
    base_name = _get_archive_base_name(archive_path)
    sibling_dir = archive_path.parent / base_name
    if not sibling_dir.is_dir():
        return False
    return any(
        p.suffix.lower() in AUDIO_EXTENSIONS
        for p in sibling_dir.rglob("*")
        if p.is_file()
    )


def extract_archive_in_place(archive_path: Path) -> Path:
    """Extract *archive_path* into a sibling folder named after its base name.

    If the sibling folder already contains audio files the extraction is
    skipped — making this call idempotent across multiple scan runs.

    Returns the extraction directory path (whether newly created or
    pre-existing).

    Supports: ZIP, RAR, 7Z, TAR, TAR.GZ, TAR.BZ2.
    Handles multi-part extensions like .tar.gz correctly.
    """
    base_name = _get_archive_base_name(archive_path)
    dest_dir = archive_path.parent / base_name
    if is_archive_already_extracted(archive_path):
        logger.debug("Archive already extracted, skipping: %s", archive_path)
        return dest_dir
    extract_archive(archive_path, dest_dir)
    logger.info("Pre-extracted archive in-place: %s -> %s", archive_path, dest_dir)
    return dest_dir


# ---------------------------------------------------------------------------
# Companion file discovery
# ---------------------------------------------------------------------------


def find_companions(audio_folder: Path, companion_extensions: list[str]) -> list[Path]:
    """Find companion files and folders in *audio_folder*.

    *audio_folder* is the direct parent directory of one or more audio files
    (the "top-level" album folder).

    Rules:
      - Any FILE directly inside *audio_folder* whose extension (lowercased)
        is in *companion_extensions* is included — provided it is not itself
        an audio file or archive.
      - Any immediate SUBFOLDER of *audio_folder* that contains **no** audio
        files at any depth is treated as a companion folder; ALL files inside
        it are included recursively.
      - Immediate subfolders that DO contain audio files are skipped (they are
        separate audio groups, not companions of this folder).

    Returns a deduplicated, sorted list of Path objects (files only).
    """
    companions: list[Path] = []
    ext_set = frozenset(e.lower() for e in companion_extensions)

    if not audio_folder.is_dir():
        return companions

    for child in audio_folder.iterdir():
        if child.is_file():
            ext = child.suffix.lower()
            if ext in ext_set and ext not in AUDIO_EXTENSIONS and ext not in ARCHIVE_EXTENSIONS:
                companions.append(child)
        elif child.is_dir():
            # Check whether this subfolder contains any audio files
            has_audio = any(
                p.suffix.lower() in AUDIO_EXTENSIONS
                for p in child.rglob("*")
                if p.is_file()
            )
            if not has_audio:
                # Companion folder — include every file inside recursively
                companions.extend(p for p in child.rglob("*") if p.is_file())

    return sorted(set(companions))


def fmt_size(size: int) -> str:
    """Human-readable file size string (B, KB, MB, GB, TB)."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size //= 1024
    return f"{size:.1f} TB"
