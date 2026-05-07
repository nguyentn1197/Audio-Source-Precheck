"""Logging configuration for MusicFlow.

Sets up a rotating file logger writing to %APPDATA%/MusicFlow/musicflow.log
alongside a console handler for development.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_APP_NAME = "MusicFlow"
_LOG_FILENAME = "musicflow.log"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 3
_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _get_log_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    log_dir = Path(appdata) / _APP_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / _LOG_FILENAME


def configure_logging(level: int = logging.DEBUG) -> None:
    """Configure root logging once.  Safe to call multiple times."""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    # Rotating file handler
    try:
        file_handler = RotatingFileHandler(
            _get_log_path(),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        # If we can't write logs, continue without crashing
        logging.warning("Could not open log file: %s", exc)

    # Console handler (INFO and above for cleaner output)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger.  Call configure_logging() first."""
    return logging.getLogger(name)
