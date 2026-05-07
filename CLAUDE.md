# MusicFlow — Agent Instructions

This document is the authoritative guide for AI coding agents working on this repository.
Read this before making any changes.

---

## Project Overview

**MusicFlow** is a Windows desktop application (Python 3.11 + PySide6/Qt) that organizes
music downloads from Torrent and Soulseek (SLSK) sources through two staging areas before
final tagging with MusicBrainz Picard.

---

## Key Invariants (Never Violate)

1. **Never delete Torrent source files.** Files from the Torrent source folder must only be
   *copied* to Staging 1. The originals must remain untouched for seeding.
2. **Soulseek files are moved** (not copied) — originals are deleted after successful move.
3. **All destructive file operations** (delete, move) must go through `musicflow/utils/file_utils.py`
   helpers, never raw `os.remove` or `shutil.move` calls in business logic.
4. **Never block the Qt main thread.** All I/O, analysis, and network calls run in
   `QThread` worker subclasses that emit signals back to the UI.
5. **MusicBrainz rate limit:** maximum 1 request per second. Always use the rate-limited
   client in `core/musicbrainz.py`; never call `musicbrainzngs` directly from other modules.

---

## Architecture

```
musicflow/
├── main.py                     # QApplication entry point
├── config.py                   # AppConfig dataclass, load/save JSON
├── ui/
│   ├── main_window.py          # QMainWindow, menu, toolbar, tab widget
│   ├── settings_dialog.py      # Folder path configuration dialog
│   ├── staging_panel.py        # Ingest tab (scan sources, ingest, view staging-1)
│   ├── analysis_panel.py       # Analysis tab (duplicates + fake hi-res)
│   └── widgets/
│       ├── file_table.py       # Reusable QTableWidget with sorting/filtering
│       └── spectrum_viewer.py  # Matplotlib FFT chart embedded in Qt
└── core/
    ├── ingest.py               # Scan, move/copy, ZIP extraction
    ├── metadata.py             # mutagen tag reading → TrackMetadata
    ├── musicbrainz.py          # musicbrainzngs wrapper, rate limiting, caching
    ├── duplicate_detector.py   # Album-based duplicate grouping
    ├── fake_hires.py           # FFT spectrum analysis
    └── export.py               # Move selected files to Staging 2
utils/
    ├── file_utils.py           # Safe file operation wrappers
    └── logging_utils.py        # Rotating file logger setup
tests/
    ├── test_ingest.py
    ├── test_metadata.py
    ├── test_duplicate_detector.py
    └── test_fake_hires.py
```

---

## Coding Conventions

- **Python 3.11+** — use `match`/`case`, `tomllib`, `StrEnum` where appropriate.
- **Type hints everywhere** — all public functions must have full type annotations.
- **Dataclasses** for data transfer objects (`TrackMetadata`, `StagedItem`, `DuplicateGroup`, etc.).
- **No global mutable state** — pass config/state explicitly.
- **Qt signals/slots** for cross-thread communication; never use `QApplication.processEvents()` as a workaround.
- **Logging** via `logging_utils.get_logger(__name__)` — never use `print()` for diagnostics.
- Line length: 100 characters (enforced by ruff).
- Imports: stdlib → third-party → local (ruff `I` rules).

---

## Running the Application

```powershell
# Activate venv first
.\.venv\Scripts\Activate.ps1

# Run
python -m musicflow.main
```

## Running Tests

```powershell
pytest
pytest tests/test_ingest.py -v   # specific module
```

## Lint & Type Check

```powershell
ruff check musicflow tests
mypy musicflow
```

---

## Adding New Features

1. Core logic goes in `musicflow/core/` — pure Python, no Qt imports.
2. Worker threads go in the same core module as a `*Worker(QThread)` class.
3. UI code goes in `musicflow/ui/` — only imports from `core/` and `config.py`.
4. Add tests in `tests/` for all core logic.
5. Update `AGENTS.md` and `CLAUDE.md` if the architecture changes.

---

## Audio Format Support

Supported formats: `.flac`, `.mp3`, `.aac`, `.m4a`, `.wav`, `.aiff`, `.ogg`, `.opus`

ZIP files are extracted in-place; nested ZIPs are extracted recursively (max depth 3).

---

## Fake Hi-Res Detection Algorithm

Located in `core/fake_hires.py`. Uses FFT to find the highest frequency with significant
energy (threshold: -60 dBFS relative to peak). A file is flagged as suspect if:

```
actual_cutoff_hz < (sample_rate / 2) * 0.85
```

Only the first 30 seconds of audio are analyzed for performance. Results include the full
spectrum array for display in the `spectrum_viewer.py` widget.
