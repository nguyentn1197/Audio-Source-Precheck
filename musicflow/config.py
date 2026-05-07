"""Application configuration for MusicFlow.

Settings are persisted as JSON in %APPDATA%/MusicFlow/config.json.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

_DEFAULT_COMPANION_EXTENSIONS: list[str] = [
    # Cover art
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp",
    # Booklets, logs, cue sheets, lyrics
    ".pdf", ".txt", ".nfo", ".log", ".cue", ".lrc",
    # Playlists
    ".m3u", ".m3u8",
    # Videos
    ".mkv", ".mp4", ".avi", ".mov", ".wmv",
]

from musicflow.utils.logging_utils import get_logger

logger = get_logger(__name__)

_APP_NAME = "MusicFlow"


def _config_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    config_dir = Path(appdata) / _APP_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "config.json"


@dataclass
class AppConfig:
    """All user-configurable folder paths and preferences."""

    torrent_source_folder: str = ""
    soulseek_source_folder: str = ""
    staging_folder_1: str = ""
    staging_folder_2: str = ""

    # Analysis preferences
    fake_hires_threshold: float = 0.85  # flag if cutoff < nyquist * threshold
    fake_hires_analysis_seconds: float = 30.0
    fake_hires_db_floor: float = -60.0  # dBFS threshold for "significant energy"
    fake_hires_cutoff_ratio_threshold: float = 0.50
    fake_hires_slope_threshold_db_oct: float = 80.0

    # Companion files — extensions to carry alongside audio during ingest and export
    companion_extensions: list[str] = field(
        default_factory=lambda: list(_DEFAULT_COMPANION_EXTENSIONS)
    )

    # Extension filtering for ingest
    deselect_extensions: list[str] = field(
        default_factory=lambda: [".nfo", ".log", ".cue", ".rar", ".zip", ".txt"]
    )
    select_extensions: list[str] = field(
        default_factory=lambda: [".flac", ".mp3", ".aac", ".m4a", ".wav"]
    )

    # MusicBrainz
    musicbrainz_user_agent: str = "MusicFlow/0.1 (https://github.com/user/musicflow)"

    def torrent_path(self) -> Path | None:
        return Path(self.torrent_source_folder) if self.torrent_source_folder else None

    def soulseek_path(self) -> Path | None:
        return Path(self.soulseek_source_folder) if self.soulseek_source_folder else None

    def staging1_path(self) -> Path | None:
        return Path(self.staging_folder_1) if self.staging_folder_1 else None

    def staging2_path(self) -> Path | None:
        return Path(self.staging_folder_2) if self.staging_folder_2 else None

    def validate(self) -> list[str]:
        """Return a list of validation warning strings (empty = all OK)."""
        warnings: list[str] = []
        checks = {
            "Torrent source folder": self.torrent_path(),
            "Soulseek source folder": self.soulseek_path(),
            "Staging folder 1": self.staging1_path(),
            "Staging folder 2": self.staging2_path(),
        }
        for label, path in checks.items():
            if path is None:
                warnings.append(f"{label} is not configured.")
            elif not path.exists():
                warnings.append(f"{label} does not exist: {path}")
        return warnings


def load_config() -> AppConfig:
    """Load config from disk, returning defaults if file is missing or corrupt."""
    path = _config_path()
    if not path.exists():
        logger.info("No config file found at %s — using defaults.", path)
        return AppConfig()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        config = AppConfig(**{k: v for k, v in data.items() if k in AppConfig.__dataclass_fields__})
        logger.info("Config loaded from %s", path)
        return config
    except Exception as exc:
        logger.warning("Failed to load config (%s) — using defaults.", exc)
        return AppConfig()


def save_config(config: AppConfig) -> None:
    """Persist *config* to disk."""
    path = _config_path()
    with path.open("w", encoding="utf-8") as fh:
        json.dump(asdict(config), fh, indent=2)
    logger.info("Config saved to %s", path)
