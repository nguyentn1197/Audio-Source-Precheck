"""Logs panel — Real-time log viewer with filtering and search.

Displays application logs with color coding by severity level.
Thread-safe: uses Qt signals for cross-thread communication.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class LogsPanel(QWidget):
    """Display real-time logs with filtering and search."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._log_messages: list[tuple[str, str, float]] = []  # (level, msg, timestamp)
        self._max_messages = 1000
        self._current_level_filter = "DEBUG"
        self._setup_ui()
        self._setup_logging()

    def _setup_ui(self) -> None:
        """Build the UI layout."""
        layout = QVBoxLayout(self)

        # Toolbar
        toolbar = QHBoxLayout()

        # Level filter dropdown
        self._level_combo = QComboBox()
        self._level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self._level_combo.currentTextChanged.connect(self._on_level_changed)
        toolbar.addWidget(self._level_combo)

        # Clear button
        clear_btn = QPushButton("Clear Logs")
        clear_btn.clicked.connect(self._clear_logs)
        toolbar.addWidget(clear_btn)

        toolbar.addStretch()
        layout.addLayout(toolbar)

        # Log display
        self._log_display = QTextEdit()
        self._log_display.setReadOnly(True)
        self._log_display.setStyleSheet(
            """
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                font-family: 'Courier New';
                font-size: 9pt;
            }
        """
        )
        layout.addWidget(self._log_display)

    def _setup_logging(self) -> None:
        """Add custom handler to capture logs."""
        # Create a signal emitter that lives in the main thread
        self._log_emitter = LogEmitter()
        self._log_emitter.log_signal.connect(self._on_log_message)

        # Create handler that uses the signal emitter
        handler = QLogHandler(self._log_emitter)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logging.getLogger().addHandler(handler)

    def _on_log_message(self, level: str, msg: str) -> None:
        """Called when a log message is emitted (always in main thread)."""
        self._log_messages.append((level, msg, time.time()))
        if len(self._log_messages) > self._max_messages:
            self._log_messages.pop(0)
        self._update_display()

    def _on_level_changed(self, level: str) -> None:
        """Called when the level filter changes."""
        self._current_level_filter = level
        self._update_display()

    def _update_display(self) -> None:
        """Update text display with colored messages."""
        # Filter messages by level
        level_order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}
        current_level_order = level_order.get(self._current_level_filter, 0)

        html = ""
        for level, msg, _ in self._log_messages:
            msg_level_order = level_order.get(level, 0)
            if msg_level_order < current_level_order:
                continue  # Skip messages below the filter level

            color = {
                "DEBUG": "#888888",
                "INFO": "#4ec9b0",
                "WARNING": "#dcdcaa",
                "ERROR": "#f48771",
            }.get(level, "#d4d4d4")
            html += f'<span style="color: {color}">{msg}</span><br>'

        self._log_display.setHtml(html)

        # Auto-scroll to bottom
        scrollbar = self._log_display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _clear_logs(self) -> None:
        """Clear all log messages."""
        self._log_messages.clear()
        self._log_display.clear()


class LogEmitter(QObject):
    """Qt signal emitter for thread-safe logging.

    Lives in the main thread and emits signals that can be safely
    connected to UI slots, even when log messages come from worker threads.
    """

    log_signal = Signal(str, str)  # level, msg

    def emit_log(self, level: str, msg: str) -> None:
        """Emit a log message signal (thread-safe)."""
        self.log_signal.emit(level, msg)


class QLogHandler(logging.Handler):
    """Qt-compatible logging handler that emits log messages via signals.

    Safe for use from multiple threads because it uses Qt signals
    for cross-thread communication.
    """

    def __init__(self, emitter: LogEmitter) -> None:
        super().__init__()
        self.emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record by emitting a Qt signal."""
        try:
            msg = self.format(record)
            self.emitter.emit_log(record.levelname, msg)
        except Exception:
            # Silently ignore errors in logging to prevent infinite loops
            pass
