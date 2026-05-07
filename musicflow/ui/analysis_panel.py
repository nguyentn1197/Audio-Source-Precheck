"""Analysis panel — duplicate detection + fake hi-res detection tab."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from musicflow.config import AppConfig
from musicflow.core.duplicate_detector import (
    AlbumInstance,
    DuplicateGroup,
    build_album_instances,
    detect_duplicates,
)
from musicflow.core.fake_hires import FakeHiResWorker, SpectrumResult
from musicflow.core.metadata import TrackMetadata, group_by_album, read_metadata_batch
from musicflow.core.musicbrainz import MBAlbumInfo, MusicBrainzWorker
from musicflow.ui.widgets.spectrum_viewer import SpectrumViewer
from musicflow.utils.file_utils import AUDIO_EXTENSIONS
from musicflow.utils.logging_utils import get_logger

logger = get_logger(__name__)


class AnalysisPanel(QWidget):
    """Analysis tab: run duplicate detection and fake hi-res scan."""

    # Emitted when analysis is complete so the export tab can refresh
    analysis_updated = Signal(list, dict)  # list[TrackMetadata], dict[Path, SpectrumResult]

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._tracks: list[TrackMetadata] = []
        self._spectrum_results: dict[Path, SpectrumResult] = {}
        self._duplicate_groups: list[DuplicateGroup] = []
        self._mb_results: dict[tuple[str, str, str], MBAlbumInfo | None] = {}
        self._mb_worker: MusicBrainzWorker | None = None
        self._hires_worker: FakeHiResWorker | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Toolbar
        toolbar = QHBoxLayout()

        self._load_btn = QPushButton("Load Staging 1")
        self._load_btn.clicked.connect(self._load_staging)
        toolbar.addWidget(self._load_btn)

        self._dup_btn = QPushButton("Run Duplicate Analysis")
        self._dup_btn.setEnabled(False)
        self._dup_btn.clicked.connect(self._run_duplicate_analysis)
        toolbar.addWidget(self._dup_btn)

        self._hires_btn = QPushButton("Run Fake Hi-Res Scan")
        self._hires_btn.setEnabled(False)
        self._hires_btn.clicked.connect(self._run_hires_scan)
        toolbar.addWidget(self._hires_btn)

        toolbar.addStretch()
        self._status_label = QLabel("Load Staging 1 to begin analysis")
        toolbar.addWidget(self._status_label)
        layout.addLayout(toolbar)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # Main splitter: duplicate tree | spectrum viewer
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: duplicate tree
        dup_group = QGroupBox("Duplicate Albums")
        dup_layout = QVBoxLayout(dup_group)
        self._dup_tree = QTreeWidget()
        self._dup_tree.setHeaderLabels(["Album / Version", "Format", "Tracks", "Reason"])
        self._dup_tree.setColumnWidth(0, 280)
        self._dup_tree.setColumnWidth(1, 120)
        self._dup_tree.itemClicked.connect(self._on_dup_item_clicked)
        dup_layout.addWidget(self._dup_tree)
        splitter.addWidget(dup_group)

        # Right: spectrum viewer
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        hires_group = QGroupBox("Spectrum Viewer")
        hires_layout = QVBoxLayout(hires_group)
        self._spectrum_viewer = SpectrumViewer()
        hires_layout.addWidget(self._spectrum_viewer)
        right_layout.addWidget(hires_group)

        # Fake hi-res summary
        self._hires_summary = QLabel("Fake hi-res scan: not run")
        right_layout.addWidget(self._hires_summary)

        splitter.addWidget(right_widget)
        splitter.setSizes([400, 500])
        layout.addWidget(splitter, stretch=1)

    def refresh_config(self, config: AppConfig) -> None:
        self._config = config

    def load_from_ingest(self, staged_paths: list[Path]) -> None:
        """Load tracks from a list of already-staged paths (called after ingest)."""
        self._tracks = read_metadata_batch(staged_paths)
        self._on_tracks_loaded()

    def _load_staging(self) -> None:
        staging1 = self._config.staging1_path()
        if not staging1 or not staging1.exists():
            QMessageBox.warning(self, "Error", "Staging 1 folder is not configured or does not exist.")
            return
        paths = [p for p in staging1.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS]
        if not paths:
            QMessageBox.information(self, "Empty", "No audio files found in Staging 1.")
            return
        self._status_label.setText(f"Reading metadata for {len(paths)} files…")
        self._tracks = read_metadata_batch(paths)
        self._on_tracks_loaded()

    def _on_tracks_loaded(self) -> None:
        self._status_label.setText(f"{len(self._tracks)} tracks loaded")
        self._dup_btn.setEnabled(bool(self._tracks))
        self._hires_btn.setEnabled(bool(self._tracks))
        self.analysis_updated.emit(self._tracks, self._spectrum_results)

    def _run_duplicate_analysis(self) -> None:
        if not self._tracks:
            return
        self._dup_btn.setEnabled(False)
        self._status_label.setText("Looking up albums on MusicBrainz…")
        self._progress.setVisible(True)
        self._progress.setValue(0)

        album_groups = group_by_album(self._tracks)
        keys = list(album_groups.keys())

        self._mb_worker = MusicBrainzWorker(keys, self._config.musicbrainz_user_agent)
        self._mb_worker.album_resolved.connect(self._on_album_resolved)
        self._mb_worker.progress.connect(lambda c, t: (
            self._progress.setMaximum(t),
            self._progress.setValue(c),
        ))
        self._mb_worker.finished.connect(lambda: self._finish_duplicate_analysis(album_groups))
        self._mb_worker.error.connect(self._on_error)
        self._mb_worker.start()

    def _on_album_resolved(self, key: tuple[str, str, str], info: object) -> None:
        self._mb_results[key] = info  # type: ignore[assignment]

    def _finish_duplicate_analysis(self, album_groups: dict) -> None:
        self._progress.setVisible(False)
        self._dup_btn.setEnabled(True)

        instances = build_album_instances(album_groups, self._mb_results)
        self._duplicate_groups = detect_duplicates(instances)

        self._populate_dup_tree()
        dup_count = len(self._duplicate_groups)
        self._status_label.setText(
            f"Duplicate analysis done: {dup_count} duplicate group(s) found"
        )
        # Mark duplicate paths for export tab
        dup_paths: set[Path] = set()
        for grp in self._duplicate_groups:
            for inst in grp.instances:
                for t in inst.tracks:
                    dup_paths.add(t.path)
        self.analysis_updated.emit(self._tracks, self._spectrum_results)

    def _populate_dup_tree(self) -> None:
        self._dup_tree.clear()
        for group in self._duplicate_groups:
            group_node = QTreeWidgetItem([
                group.display_label,
                "",
                "",
                group.reason.value,
            ])
            group_node.setForeground(0, Qt.GlobalColor.yellow)
            for inst in group.instances:
                child = QTreeWidgetItem([
                    str(inst.folder.name),
                    inst.display_quality,
                    str(inst.track_count),
                    f"{group.confidence:.0%}",
                ])
                child.setData(0, Qt.ItemDataRole.UserRole, inst)
                group_node.addChild(child)
            self._dup_tree.addTopLevelItem(group_node)
        self._dup_tree.expandAll()

    def _run_hires_scan(self) -> None:
        if not self._tracks:
            return
        paths = [t.path for t in self._tracks if t.path.exists()]
        self._hires_btn.setEnabled(False)
        self._status_label.setText(f"Scanning {len(paths)} files for fake hi-res…")
        self._progress.setVisible(True)
        self._progress.setValue(0)

        self._hires_worker = FakeHiResWorker(
            paths=paths,
            threshold=self._config.fake_hires_threshold,
            db_floor=self._config.fake_hires_db_floor,
            analysis_seconds=self._config.fake_hires_analysis_seconds,
        )
        self._hires_worker.file_analyzed.connect(self._on_file_analyzed)
        self._hires_worker.progress.connect(self._on_hires_progress)
        self._hires_worker.finished.connect(self._on_hires_done)
        self._hires_worker.error.connect(self._on_error)
        self._hires_worker.start()

    def _on_hires_progress(self, current: int, total: int, filename: str) -> None:
        self._progress.setMaximum(total)
        self._progress.setValue(current)
        self._status_label.setText(f"Analysing: {filename}")

    def _on_file_analyzed(self, result: SpectrumResult) -> None:
        self._spectrum_results[result.path] = result
        self.analysis_updated.emit(self._tracks, self._spectrum_results)

    def _on_hires_done(self) -> None:
        self._progress.setVisible(False)
        self._hires_btn.setEnabled(True)
        suspect_count = sum(1 for r in self._spectrum_results.values() if r.is_suspect)
        total = len(self._spectrum_results)
        self._hires_summary.setText(
            f"Fake hi-res scan: {suspect_count} suspect / {total} scanned"
        )
        self._status_label.setText(f"Scan complete — {suspect_count} suspect file(s)")

    def _on_dup_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        inst: AlbumInstance | None = item.data(0, Qt.ItemDataRole.UserRole)
        if inst and inst.tracks:
            # Show spectrum of first track in the instance
            first_track = inst.tracks[0]
            result = self._spectrum_results.get(first_track.path)
            if result:
                self._spectrum_viewer.show_result(result)

    def show_spectrum(self, track: TrackMetadata) -> None:
        """Show spectrum for a specific track (called from export tab)."""
        result = self._spectrum_results.get(track.path)
        if result:
            self._spectrum_viewer.show_result(result)
        else:
            self._spectrum_viewer.clear()

    def _on_error(self, msg: str) -> None:
        self._progress.setVisible(False)
        self._dup_btn.setEnabled(True)
        self._hires_btn.setEnabled(True)
        QMessageBox.critical(self, "Analysis Error", msg)
