# MusicFlow

A Windows desktop application to organize music downloads from Torrent and Soulseek (SLSK) sources.

## Workflow

```
Torrent source folder  ──┐
                          ├──► Staging 1 (analysis) ──► Staging 2 (ready for Picard)
Soulseek source folder ──┘
```

1. **Ingest** — Scan source folders, extract ZIPs, move/copy files to Staging 1.
   - Torrent files are **copied** (originals kept for seeding).
   - Soulseek files are **moved** (originals deleted).
2. **Analyze** — Detect album duplicates using file metadata + MusicBrainz lookups.
3. **Fake Hi-Res Detection** — FFT spectrum analysis flags files that are upsampled from lower quality.
4. **Export** — User selects files to keep; they are moved to Staging 2 for tagging with MusicBrainz Picard.

## Requirements

- Python 3.11+
- Windows 10/11

## Setup

```powershell
# Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Run the application
python -m musicflow.main
# or after pip install -e .
musicflow
```

## Development

```powershell
pip install -r requirements-dev.txt

# Lint
ruff check musicflow tests

# Type check
mypy musicflow

# Tests
pytest
```

## Project Structure

```
musicflow/
├── main.py                  # Entry point
├── config.py                # App settings (folder paths)
├── ui/
│   ├── main_window.py       # QMainWindow shell
│   ├── settings_dialog.py   # Folder configuration dialog
│   ├── staging_panel.py     # Ingest tab
│   ├── analysis_panel.py    # Duplicate + fake hi-res tab
│   └── widgets/
│       ├── file_table.py    # Reusable sortable file table
│       └── spectrum_viewer.py  # Matplotlib spectrum widget
└── core/
    ├── ingest.py            # Move/copy/extract pipeline
    ├── metadata.py          # Audio tag reading (mutagen)
    ├── musicbrainz.py       # MusicBrainz API client
    ├── duplicate_detector.py
    ├── fake_hires.py        # FFT-based fake hi-res detection
    └── export.py            # Move to Staging 2
```
