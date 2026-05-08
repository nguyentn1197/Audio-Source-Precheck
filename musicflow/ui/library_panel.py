"""LibraryPanel — unified Artist → Release → Song → file tree with export."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from musicflow.core.export import export_to_staging2
from musicflow.core.fake_hires import SpectrumResult
from musicflow.core.ingest import (
    LoadFromStagingWorker,
    ReanalyseWorker,
    RescanWorker,
    StagedItem,
)
from musicflow.core.metadata import TrackMetadata
from musicflow.ui.widgets.spectrum_viewer import SpectrumViewer
from musicflow.utils.file_utils import fmt_size, open_in_explorer, safe_delete

if TYPE_CHECKING:
    from musicflow.config import AppConfig

logger = logging.getLogger(__name__)

_COL_NAME = 0
_COL_FORMAT = 1
_COL_HIRES = 2
_COL_STATUS = 3

_ITEM_PATH_ROLE = Qt.ItemDataRole.UserRole
_SONG_TITLE_ROLE = Qt.ItemDataRole.UserRole + 1
_SONG_TRACK_ROLE = Qt.ItemDataRole.UserRole + 2


class LibraryPanel(QWidget):
    """Three-level tree: Artist → Release → Song → file rows.

    Receives file_ready signals from IngestAnalysisWorker and updates live.
    """

    export_completed = Signal(object)  # ExportResult

    def __init__(self, config: "AppConfig", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._spectrum_cache: dict[Path, SpectrumResult] = {}
        self._meta_cache: dict[Path, TrackMetadata] = {}
        self._file_row_cache: dict[Path, QTreeWidgetItem] = {}
        self._active_workers: list[object] = []
        self._setup_ui()

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: tree
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["Name", "Format / Size", "Hi-Res", "Status"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        left_layout.addWidget(self._tree)

        btn_row = QHBoxLayout()
        self._export_btn = QPushButton("Move selected to Staging 2")
        self._export_btn.clicked.connect(self._on_export)
        self._delete_btn = QPushButton("Delete selected file(s)")
        self._delete_btn.clicked.connect(self._on_delete_selected)
        self._load_btn = QPushButton("Load from Staging 1")
        self._load_btn.clicked.connect(self._on_load_from_staging)
        self._status_label = QLabel("")
        btn_row.addWidget(self._export_btn)
        btn_row.addWidget(self._delete_btn)
        btn_row.addWidget(self._load_btn)
        btn_row.addWidget(self._status_label)
        btn_row.addStretch()
        left_layout.addLayout(btn_row)

        splitter.addWidget(left)

        # Right: metadata + spectrum viewer
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self._meta_label = QLabel("")
        self._meta_label.setWordWrap(True)
        self._spectrum_viewer = SpectrumViewer()
        right_layout.addWidget(self._meta_label)
        right_layout.addWidget(self._spectrum_viewer)
        splitter.addWidget(right)

        splitter.setSizes([600, 400])
        layout.addWidget(splitter)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Clear tree and caches. Call before a new ingest."""
        self._tree.clear()
        self._spectrum_cache.clear()
        self._meta_cache.clear()
        self._file_row_cache.clear()
        self._spectrum_viewer.clear()
        self._meta_label.clear()

    def load_from_staging(self) -> None:
        self._load_btn.setEnabled(False)
        self._status_label.setText("Loading from Staging 1...")
        self.clear()
        worker = LoadFromStagingWorker(self._config.staging1_path(), self._config)
        worker.file_meta_ready.connect(self.on_file_meta_ready)
        worker.file_analysis_ready.connect(self.on_file_analysis_ready)
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.error.connect(self._on_worker_error)
        self._analysis_worker = worker
        self._active_workers.append(worker)
        worker.finished.connect(lambda: self._active_workers.remove(worker) if worker in self._active_workers else None)
        worker.start()

    def _on_load_from_staging(self) -> None:
        self.load_from_staging()

    def _on_worker_progress(self, current: int, total: int, filename: str) -> None:
        self._status_label.setText(f"Working [{current}/{total}]: {filename}")

    def _on_worker_finished(self) -> None:
        self._load_btn.setEnabled(True)
        self._status_label.setText("Ready")

    def _on_worker_error(self, msg: str) -> None:
        self._load_btn.setEnabled(True)
        QMessageBox.critical(self, "Library Worker Error", msg)

    def on_file_meta_ready(self, staged: StagedItem, meta: TrackMetadata) -> None:
        self._meta_cache[staged.path] = meta
        self._upsert_file_row(staged, meta, None)

    def on_file_analysis_ready(self, path: Path, spectrum: SpectrumResult | None) -> None:
        if spectrum is not None:
            self._spectrum_cache[path] = spectrum
        row = self._file_row_cache.get(path)
        if row is None:
            return
        if spectrum is None:
            hires_text = "–"
        elif spectrum.hi_res_verdict is not None:
            hires_text = spectrum.hi_res_verdict.label
        elif spectrum.is_suspect:
            hires_text = "⚠ SUSPECT"
        else:
            hires_text = "✓ OK"
        row.setText(_COL_HIRES, hires_text)
        row.setText(_COL_STATUS, "Analyzed" if spectrum else "Analysis failed")

    def refresh_config(self, config: "AppConfig") -> None:
        self._config = config

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self._tree.itemAt(pos)
        if item is None:
            return
        paths = self._collect_audio_paths(item)
        if not paths:
            return
        menu = QMenu(self)
        menu.addAction("Rescan", lambda: self._start_rescan(paths, False))
        menu.addAction("Rescan with MusicBrainz", lambda: self._start_rescan(paths, True))
        menu.addAction("Re-analyse", lambda: self._start_reanalyse(paths))
        if len(paths) == 1:
            menu.addAction("Open directory", lambda: open_in_explorer(paths[0].parent))
            if getattr(self, "_analysis_worker", None) is not None:
                menu.addAction("Abort analysis", lambda: self._analysis_worker.abort_paths(paths))
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _collect_audio_paths(self, item: QTreeWidgetItem) -> list[Path]:
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(path, Path) and path.is_file():
            return [path]
        paths: list[Path] = []
        for idx in range(item.childCount()):
            paths.extend(self._collect_audio_paths(item.child(idx)))
        return paths

    def _start_rescan(self, paths: list[Path], use_musicbrainz: bool) -> None:
        worker = RescanWorker(paths, self._config, use_musicbrainz)
        worker.file_rescanned.connect(self._on_rescanned_file)
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.error.connect(self._on_worker_error)
        self._active_workers.append(worker)
        worker.finished.connect(lambda: self._active_workers.remove(worker) if worker in self._active_workers else None)
        worker.start()

    def _start_reanalyse(self, paths: list[Path]) -> None:
        for path in paths:
            row = self._file_row_cache.get(path)
            if row is not None:
                row.setText(_COL_STATUS, "Re-analysing...")
        worker = ReanalyseWorker(paths, self._config)
        worker.file_reanalysed.connect(self._on_reanalysed_file)
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.error.connect(self._on_worker_error)
        self._active_workers.append(worker)
        worker.finished.connect(lambda: self._active_workers.remove(worker) if worker in self._active_workers else None)
        worker.start()

    def _on_rescanned_file(self, staged: StagedItem, meta: TrackMetadata, spectrum: SpectrumResult | None) -> None:
        self._meta_cache[staged.path] = meta
        if spectrum is not None:
            self._spectrum_cache[staged.path] = spectrum
        self._update_or_move_file_row(staged, meta, spectrum)

    def _on_reanalysed_file(self, staged: StagedItem, meta: TrackMetadata, spectrum: SpectrumResult | None) -> None:
        self._meta_cache[staged.path] = meta
        if spectrum is not None:
            self._spectrum_cache[staged.path] = spectrum
        row = self._file_row_cache.get(staged.path)
        if row is None:
            return
        row.setText(_COL_HIRES, "⚠ SUSPECT" if (spectrum and spectrum.is_suspect) else ("✓ OK" if spectrum else "–"))
        row.setText(_COL_STATUS, "Analyzed" if spectrum else "Analysis failed")

    def on_mb_folder_renamed(self, old_path: Path, new_path: Path) -> None:
        """Update tree nodes when MBRenameWorker renames an album folder."""
        # Find album items whose stored path starts with old_path
        for artist_idx in range(self._tree.topLevelItemCount()):
            artist_item = self._tree.topLevelItem(artist_idx)
            for album_idx in range(artist_item.childCount()):
                album_item = artist_item.child(album_idx)
                folder: Path = album_item.data(0, Qt.ItemDataRole.UserRole)
                if folder == old_path:
                    album_item.setData(0, Qt.ItemDataRole.UserRole, new_path)
                    album_item.setText(0, new_path.name)

    # ------------------------------------------------------------------
    # Tree helpers
    # ------------------------------------------------------------------

    def _upsert_file_row(
        self,
        staged: StagedItem,
        meta: TrackMetadata,
        spectrum: SpectrumResult | None,
    ) -> None:
        artist = (meta.album_artist or meta.artist or "Unknown Artist").strip()
        album = (meta.album or "Unknown Album").strip()
        year = (meta.date or "")[:4]
        title = (meta.title or staged.path.stem).strip()

        artist_item = self._find_or_create_artist(artist)
        album_item = self._find_or_create_album(
            artist_item, album, year, staged.path.parent.parent
        )
        song_item = self._find_or_create_song(album_item, title, meta.track_number)
        self._add_file_row(song_item, staged, meta, spectrum)

        # Update song badge
        count = song_item.childCount()
        song_item.setText(0, f"{title}  [{count}]")

        self._tree.expandAll()

    def _find_or_create_artist(self, artist: str) -> QTreeWidgetItem:
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item.text(0) == artist:
                return item
        item = QTreeWidgetItem([artist, "", ""])
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        self._tree.addTopLevelItem(item)
        return item

    def _find_or_create_album(
        self,
        artist_item: QTreeWidgetItem,
        album: str,
        year: str,
        album_folder: Path,
    ) -> QTreeWidgetItem:
        label = f"{album} ({year})" if year else album
        for i in range(artist_item.childCount()):
            item = artist_item.child(i)
            if item.text(0) == label:
                return item
        item = QTreeWidgetItem([label, "", ""])
        item.setData(0, Qt.ItemDataRole.UserRole, album_folder)
        artist_item.addChild(item)
        return item

    def _find_or_create_song(
        self,
        album_item: QTreeWidgetItem,
        title: str,
        track_number: int,
    ) -> QTreeWidgetItem:
        for i in range(album_item.childCount()):
            item = album_item.child(i)
            stored_title: str = item.data(0, Qt.ItemDataRole.UserRole + 1) or ""
            if stored_title == title:
                return item
        item = QTreeWidgetItem([title, "", ""])
        item.setData(0, Qt.ItemDataRole.UserRole + 1, title)
        item.setData(0, Qt.ItemDataRole.UserRole + 2, track_number)
        album_item.addChild(item)
        return item

    def _add_file_row(
        self,
        song_item: QTreeWidgetItem,
        staged: StagedItem,
        meta: TrackMetadata,
        spectrum: SpectrumResult | None,
    ) -> None:
        source_badge = f"[{staged.source_type.value}]"
        name_text = f"{staged.path.name}  {source_badge}"
        size_text = (
            fmt_size(staged.path.stat().st_size)
            if staged.path.exists()
            else ""
        )
        fmt_text = f"{meta.display_format}  {size_text}"
        hires_text = "⚠ SUSPECT" if (spectrum and spectrum.is_suspect) else "✓ OK"

        status_text = "Analyzing..." if spectrum is None else ("Analyzed" if spectrum else "Analysis failed")
        row = QTreeWidgetItem([name_text, fmt_text, hires_text, status_text])
        row.setCheckState(0, Qt.CheckState.Unchecked)
        row.setData(0, _ITEM_PATH_ROLE, staged.path)
        row.setData(0, _SONG_TITLE_ROLE, meta.title or staged.path.stem)
        row.setData(0, _SONG_TRACK_ROLE, meta.track_number)
        self._file_row_cache[staged.path] = row
        song_item.addChild(row)

    def _update_or_move_file_row(
        self,
        staged: StagedItem,
        meta: TrackMetadata,
        spectrum: SpectrumResult | None,
    ) -> None:
        existing = self._file_row_cache.get(staged.path)
        if existing is None:
            self._upsert_file_row(staged, meta, spectrum)
            return

        new_artist = (meta.album_artist or meta.artist or "Unknown Artist").strip()
        new_album = (meta.album or "Unknown Album").strip()
        new_year = (meta.date or "")[:4]
        new_title = (meta.title or staged.path.stem).strip()

        album_item = existing.parent().parent() if existing.parent() else None
        artist_item = album_item.parent() if album_item else None
        current_artist = artist_item.text(0) if artist_item else ""
        current_album = album_item.text(0) if album_item else ""
        expected_album = f"{new_album} ({new_year})" if new_year else new_album

        needs_move = (
            current_artist != new_artist
            or current_album != expected_album
            or existing.data(0, _SONG_TITLE_ROLE) != new_title
            or staged.path != existing.data(0, _ITEM_PATH_ROLE)
        )
        if needs_move:
            if existing.parent() is not None:
                existing.parent().removeChild(existing)
            self._upsert_file_row(staged, meta, spectrum)
            return

        size_text = fmt_size(staged.path.stat().st_size) if staged.path.exists() else ""
        existing.setText(0, f"{staged.path.name}  [{staged.source_type.value}]")
        existing.setText(1, f"{meta.display_format}  {size_text}")
        existing.setText(2, "⚠ SUSPECT" if (spectrum and spectrum.is_suspect) else ("✓ OK" if spectrum else "Analysing…"))
        existing.setText(3, "Analyzed" if spectrum else "Pending")
        existing.setData(0, _ITEM_PATH_ROLE, staged.path)
        existing.setData(0, _SONG_TITLE_ROLE, new_title)
        existing.setData(0, _SONG_TRACK_ROLE, meta.track_number)
        self._file_row_cache.pop(staged.path, None)
        self._file_row_cache[staged.path] = existing


    # ------------------------------------------------------------------
    # Selection + detail view
    # ------------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            return
        item = items[0]
        path: Path | None = item.data(0, Qt.ItemDataRole.UserRole)
        if path is None or not isinstance(path, Path):
            return
        meta = self._meta_cache.get(path)
        spectrum = self._spectrum_cache.get(path)
        if meta:
            lines = [
                f"<b>{meta.title}</b>",
                f"Artist: {meta.artist}",
                f"Album: {meta.album} ({meta.date})",
                f"Track: {meta.track_number}",
                f"Format: {meta.display_format}",
                f"Duration: {meta.duration:.1f}s",
            ]
            self._meta_label.setText("<br>".join(lines))
        if spectrum:
            self._spectrum_viewer.show_result(spectrum)
        else:
            self._spectrum_viewer.clear()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export(self) -> None:
        selected_paths = self._collect_checked_paths()
        if not selected_paths:
            self._status_label.setText("No files selected.")
            return
        tracks = [
            self._meta_cache[p]
            for p in selected_paths
            if p in self._meta_cache
        ]
        staging2 = self._config.staging2_path()
        result = export_to_staging2(
            tracks, staging2, self._config.companion_extensions
        )
        self._status_label.setText(
            f"Moved {result.success_count} file(s) to Staging 2. Errors: {result.error_count}"
        )
        self.export_completed.emit(result)

    def _on_delete_selected(self) -> None:
        selected_paths = self._collect_checked_paths()
        if not selected_paths:
            self._status_label.setText("No files selected.")
            return

        # Show full paths so user knows exactly what will be deleted
        path_list = "\n".join(str(p) for p in selected_paths[:10])
        if len(selected_paths) > 10:
            path_list += f"\n… and {len(selected_paths) - 10} more"
        reply = QMessageBox.question(
            self,
            "Confirm Permanent Delete",
            f"Permanently delete {len(selected_paths)} file(s) from disk?\n"
            f"This cannot be undone.\n\n{path_list}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        deleted = 0
        errors = 0
        for path in selected_paths:
            try:
                safe_delete(path)
                # Also remove the spectrum cache NPZ to prevent stale data on re-ingest
                npz_path = path.with_stem(path.stem + ".spectrum").with_suffix(".npz")
                if npz_path.exists():
                    safe_delete(npz_path)
                self._remove_file_row(path)
                deleted += 1
            except Exception as exc:
                logger.error("Delete failed for %s: %s", path, exc)
                errors += 1

        self._status_label.setText(
            f"Deleted {deleted} file(s). Errors: {errors}"
        )

    def _collect_checked_paths(self) -> list[Path]:
        paths: list[Path] = []
        for artist_idx in range(self._tree.topLevelItemCount()):
            artist_item = self._tree.topLevelItem(artist_idx)
            for album_idx in range(artist_item.childCount()):
                album_item = artist_item.child(album_idx)
                for song_idx in range(album_item.childCount()):
                    song_item = album_item.child(song_idx)
                    for file_idx in range(song_item.childCount()):
                        file_item = song_item.child(file_idx)
                        if (
                            file_item.checkState(0)
                            == Qt.CheckState.Checked
                        ):
                            p = file_item.data(
                                0, Qt.ItemDataRole.UserRole
                            )
                            if isinstance(p, Path):
                                paths.append(p)
        return paths

    def _remove_file_row(self, path: Path) -> None:
        """Remove the tree row for *path* and clean up caches."""
        row = self._file_row_cache.pop(path, None)
        if row is None:
            return
        self._spectrum_cache.pop(path, None)
        self._meta_cache.pop(path, None)

        song_item = row.parent()
        if song_item is None:
            return
        song_item.removeChild(row)

        # Update song badge count
        title_data: str = song_item.data(0, Qt.ItemDataRole.UserRole + 1) or ""
        count = song_item.childCount()
        if count > 0:
            song_item.setText(0, f"{title_data}  [{count}]")
        else:
            # Remove empty song node
            album_item = song_item.parent()
            if album_item is not None:
                album_item.removeChild(song_item)
                # Remove empty album node
                if album_item.childCount() == 0:
                    artist_item = album_item.parent()
                    if artist_item is not None:
                        artist_item.removeChild(album_item)
                        # Remove empty artist node
                        if artist_item.childCount() == 0:
                            idx = self._tree.indexOfTopLevelItem(artist_item)
                            if idx >= 0:
                                self._tree.takeTopLevelItem(idx)
