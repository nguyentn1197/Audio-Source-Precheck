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
│   ├── main_window.py          # QMainWindow, 3-tab layout: Ingest | Library | Logs
│   ├── settings_dialog.py      # Folder paths + companion/extension config
│   ├── staging_panel.py        # Ingest tab: scan sources, ingest to Staging 1
│   ├── library_panel.py        # Library tab: Artist→Album→Song→File tree + export
│   ├── logs_panel.py           # Logs tab
│   └── widgets/
│       ├── file_table.py       # Legacy QTableWidget (unused in main flow)
│       └── spectrum_viewer.py  # Matplotlib spectrogram heatmap embedded in Qt
└── core/
    ├── ingest.py               # Scan, move/copy, ZIP extraction, IngestAnalysisWorker
    ├── metadata.py             # mutagen tag reading → TrackMetadata
    ├── musicbrainz.py          # musicbrainzngs wrapper, rate limiting, caching
    ├── fake_hires.py           # STFT spectrogram analysis + NPZ persistence
    └── export.py               # Move selected files to Staging 2
utils/
    ├── file_utils.py           # Safe file operation wrappers, fmt_size
    └── logging_utils.py        # Rotating file logger setup
tests/
    ├── test_ingest.py
    ├── test_metadata.py
    ├── test_duplicate_detector.py
    └── test_fake_hires.py
```

---

## Staging 1 Folder Structure

Files are organised as:

```
staging_1/
└── Artist/
    └── Album/
        └── torrent|soulseek/
            ├── 01 - Track.flac
            ├── 02 - Track.flac
            └── cover.jpg          ← companion files land here too
```

Album folder name is derived from audio tags (`album_artist` or `artist` + `album`).
Falls back to the source subfolder name if tags are missing.
Torrent files are idempotent — re-ingesting skips files already present.

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
5. Update this `AGENTS.md` if the architecture changes.

---

## Audio Format Support

Supported formats: `.flac`, `.mp3`, `.aac`, `.m4a`, `.wav`, `.aiff`, `.ogg`, `.opus`

ZIP files are extracted in-place; nested ZIPs are extracted recursively (max depth 3).

---

## Fake Hi-Res Detection Algorithm

Located in `core/fake_hires.py`. Uses STFT (Short-Time Fourier Transform) to produce a
time×frequency spectrogram. The highest frequency with significant energy (threshold:
−60 dBFS relative to peak) is found from the Welch PSD. A file is flagged as suspect if:

```
actual_cutoff_hz < (sample_rate / 2) * 0.85
```

Only the first 30 seconds of audio are analyzed for performance. Results include:
- `frequencies` / `power_db` — Welch PSD arrays for cutoff detection
- `spectrogram_times` / `spectrogram_freqs` / `spectrogram_db` — STFT 2D arrays for display

The spectrogram is displayed as a heatmap in `spectrum_viewer.py` (inferno colormap,
Y=frequency in kHz, X=time in seconds). Nyquist and cutoff frequency lines are overlaid.
Results are cached as `{stem}.spectrum.npz` alongside the staged audio file.

---
Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.