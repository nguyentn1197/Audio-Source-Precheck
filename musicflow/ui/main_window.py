"""Main application window for MusicFlow."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from musicflow.config import AppConfig, load_config, save_config
from musicflow.core.ingest import IngestAnalysisWorker, IngestResult
from musicflow.ui.library_panel import LibraryPanel
from musicflow.ui.logs_panel import LogsPanel
from musicflow.ui.settings_dialog import SettingsDialog
from musicflow.ui.staging_panel import StagingPanel
from musicflow.utils.logging_utils import get_logger

logger = get_logger(__name__)

_APP_VERSION = "0.1.0"


class MainWindow(QMainWindow):
    """Top-level application window."""

    def __init__(self) -> None:
        super().__init__()
        self._config = load_config()
        self._setup_ui()
        self._setup_menu()
        self._setup_statusbar()
        self.setWindowTitle(f"MusicFlow {_APP_VERSION}")
        self.resize(1200, 800)
        logger.info("MusicFlow started")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        # Tab 0: Ingest
        self._staging_panel = StagingPanel(self._config)
        self._staging_panel.ingest_completed.connect(self._on_ingest_completed)
        self._staging_panel.ingest_worker_ready.connect(self._on_ingest_worker_ready)
        self._tabs.addTab(self._staging_panel, "Ingest")

        # Tab 1: Library (replaces Analysis + Export)
        self._library_panel = LibraryPanel(self._config)
        self._library_panel.export_completed.connect(self._on_export_completed)
        self._tabs.addTab(self._library_panel, "Library")

        # Tab 2: Logs
        self._logs_panel = LogsPanel()
        self._tabs.addTab(self._logs_panel, "Logs")

    def _setup_menu(self) -> None:
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")

        settings_action = QAction("&Settings…", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Help menu
        help_menu = menubar.addMenu("&Help")

        about_action = QAction("&About MusicFlow", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _setup_statusbar(self) -> None:
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._status_label = QLabel("Ready")
        self._statusbar.addWidget(self._status_label)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self._config, self)
        if dlg.exec():
            self._config = dlg.get_config()
            save_config(self._config)
            self._staging_panel.refresh_config(self._config)
            self._library_panel.refresh_config(self._config)
            self._status_label.setText("Settings saved")

    def _on_ingest_completed(self, result: IngestResult) -> None:
        self._status_label.setText(
            f"Ingest done: {result.success_count} files in Staging 1"
        )
        self._tabs.setCurrentIndex(1)

    def _on_ingest_worker_ready(self, worker) -> None:
        """Called before the ingest+analysis worker starts.

        Clears the library and connects file_ready so the tree updates live
        as each file finishes analysis.
        """
        self._library_panel.clear()
        worker.file_meta_ready.connect(self._library_panel.on_file_meta_ready)
        worker.file_analysis_ready.connect(self._library_panel.on_file_analysis_ready)

    def _load_library_from_staging(self) -> None:
        self._library_panel.load_from_staging()

    def _on_export_completed(self, result) -> None:
        staging2 = self._config.staging2_path()
        if result.error_count:
            QMessageBox.warning(
                self,
                "Export Errors",
                f"{result.error_count} file(s) failed:\n"
                + "\n".join(f"• {p.name}: {e}" for p, e in result.errors[:10]),
            )
        else:
            QMessageBox.information(
                self,
                "Export Complete",
                f"{result.success_count} file(s) moved to Staging 2.\n\n"
                "You can now open MusicBrainz Picard and point it at:\n"
                f"{staging2}",
            )

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About MusicFlow",
            f"<b>MusicFlow {_APP_VERSION}</b><br><br>"
            "Music download organizer for Torrent and Soulseek sources.<br><br>"
            "Workflow:<br>"
            "1. Ingest downloads into Staging 1<br>"
            "2. Detect duplicates and fake hi-res files<br>"
            "3. Export selected files to Staging 2 for MusicBrainz Picard",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        save_config(self._config)
        logger.info("MusicFlow closed")
        super().closeEvent(event)
