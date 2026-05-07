"""Reusable sortable file table widget."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from musicflow.core.fake_hires import SpectrumResult
from musicflow.core.metadata import TrackMetadata
from musicflow.utils.file_utils import open_in_explorer


# Colour palette (Catppuccin Mocha)
_CLR_SUSPECT = QColor("#f38ba8")   # red
_CLR_DUPLICATE = QColor("#fab387")  # peach
_CLR_OK = QColor("#a6e3a1")        # green
_CLR_UNKNOWN = QColor("#cdd6f4")   # text


class FileTableItem(QTableWidgetItem):
    """Table item that stores a reference to its TrackMetadata."""

    def __init__(self, text: str, track: TrackMetadata) -> None:
        super().__init__(text)
        self.track = track


class FileTable(QTableWidget):
    """Sortable, filterable table of audio files with checkbox selection."""

    selection_changed = Signal(object)  # TrackMetadata | None

    COLUMNS = [
        "Select",
        "Filename",
        "Artist",
        "Album",
        "Format",
        "Sample Rate",
        "Bit Depth",
        "Duration",
        "Duplicate",
        "Fake Hi-Res",
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self._tracks: list[TrackMetadata] = []
        self._spectrum: dict[Path, SpectrumResult] = {}
        self._duplicate_paths: set[Path] = set()
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSortIndicatorShown(True)
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.itemSelectionChanged.connect(self._on_selection_changed)
        # Right-click context menu
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def load_tracks(
        self,
        tracks: list[TrackMetadata],
        spectrum: dict[Path, SpectrumResult] | None = None,
        duplicate_paths: set[Path] | None = None,
    ) -> None:
        """Populate the table from a list of TrackMetadata."""
        self._tracks = tracks
        self._spectrum = spectrum or {}
        self._duplicate_paths = duplicate_paths or set()
        self._rebuild()

    def update_spectrum(self, result: SpectrumResult) -> None:
        """Update a single row's fake hi-res status after analysis."""
        self._spectrum[result.path] = result
        for row in range(self.rowCount()):
            item = self.item(row, 1)
            if isinstance(item, FileTableItem) and item.track.path == result.path:
                self._fill_row(row, item.track)
                break

    def mark_duplicate(self, path: Path) -> None:
        """Mark a file as a duplicate and refresh its row."""
        self._duplicate_paths.add(path)
        for row in range(self.rowCount()):
            item = self.item(row, 1)
            if isinstance(item, FileTableItem) and item.track.path == path:
                self._fill_row(row, item.track)
                break

    def selected_tracks(self) -> list[TrackMetadata]:
        """Return tracks where the Select checkbox is checked."""
        result: list[TrackMetadata] = []
        for row in range(self.rowCount()):
            chk = self.item(row, 0)
            name_item = self.item(row, 1)
            if (
                chk is not None
                and chk.checkState() == Qt.CheckState.Checked
                and isinstance(name_item, FileTableItem)
            ):
                result.append(name_item.track)
        return result

    def select_all_clean(self) -> None:
        """Check all rows that are neither suspect nor duplicates."""
        for row in range(self.rowCount()):
            chk = self.item(row, 0)
            name_item = self.item(row, 1)
            if chk is None or not isinstance(name_item, FileTableItem):
                continue
            track = name_item.track
            is_suspect = self._spectrum.get(track.path, SpectrumResult(
                path=track.path, sample_rate=0, bit_depth=None, duration=0, channels=0,
                actual_cutoff_hz=0, nyquist_hz=0, is_suspect=False, confidence=0, reason="",
            )).is_suspect
            is_dup = track.path in self._duplicate_paths
            chk.setCheckState(
                Qt.CheckState.Unchecked if (is_suspect or is_dup) else Qt.CheckState.Checked
            )

    def deselect_suspects(self) -> None:
        """Uncheck all suspect / duplicate rows."""
        for row in range(self.rowCount()):
            chk = self.item(row, 0)
            name_item = self.item(row, 1)
            if chk is None or not isinstance(name_item, FileTableItem):
                continue
            track = name_item.track
            is_suspect = self._spectrum.get(track.path, SpectrumResult(
                path=track.path, sample_rate=0, bit_depth=None, duration=0, channels=0,
                actual_cutoff_hz=0, nyquist_hz=0, is_suspect=False, confidence=0, reason="",
            )).is_suspect
            if is_suspect or track.path in self._duplicate_paths:
                chk.setCheckState(Qt.CheckState.Unchecked)

    def _rebuild(self) -> None:
        self.setSortingEnabled(False)
        self.setRowCount(0)
        for track in self._tracks:
            row = self.rowCount()
            self.insertRow(row)
            self._fill_row(row, track)
        self.setSortingEnabled(True)

    def _fill_row(self, row: int, track: TrackMetadata) -> None:
        spectrum = self._spectrum.get(track.path)
        is_suspect = spectrum.is_suspect if spectrum else False
        is_dup = track.path in self._duplicate_paths

        # Col 0: checkbox
        chk = QTableWidgetItem()
        chk.setCheckState(Qt.CheckState.Unchecked)
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        self.setItem(row, 0, chk)

        # Col 1: filename
        name_item = FileTableItem(track.path.name, track)
        self.setItem(row, 1, name_item)

        # Remaining columns
        values: list[str] = [
            track.artist or "",
            track.album or "",
            track.format or "",
            f"{track.sample_rate // 1000}kHz" if track.sample_rate else "",
            f"{track.bit_depth}bit" if track.bit_depth else "",
            f"{track.duration:.0f}s" if track.duration else "",
            "Yes" if is_dup else "",
            spectrum.status_label if spectrum else "",
        ]
        for col_offset, val in enumerate(values, start=2):
            self.setItem(row, col_offset, QTableWidgetItem(val))

        # Row colour
        if is_suspect:
            colour = _CLR_SUSPECT
        elif is_dup:
            colour = _CLR_DUPLICATE
        else:
            colour = _CLR_UNKNOWN

        for col in range(self.columnCount()):
            item = self.item(row, col)
            if item:
                item.setForeground(colour)

    def _on_selection_changed(self) -> None:
        rows = self.selectedItems()
        if not rows:
            self.selection_changed.emit(None)
            return
        row = rows[0].row()
        name_item = self.item(row, 1)
        if isinstance(name_item, FileTableItem):
            self.selection_changed.emit(name_item.track)
        else:
            self.selection_changed.emit(None)

    def _show_context_menu(self, pos: QPoint) -> None:
        """Right-click menu: Open in File Explorer."""
        item = self.itemAt(pos)
        if item is None:
            return
        row = item.row()
        name_item = self.item(row, 1)
        if not isinstance(name_item, FileTableItem):
            return

        path = name_item.track.path
        menu = QMenu(self)
        action = menu.addAction("Open in File Explorer")
        action.triggered.connect(lambda: open_in_explorer(path))
        menu.exec(self.viewport().mapToGlobal(pos))
