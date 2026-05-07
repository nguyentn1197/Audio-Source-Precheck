"""Settings dialog — configure source and staging folder paths."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from musicflow.config import AppConfig


class _FolderRow(QWidget):
    """A label + line edit + browse button for a single folder path."""

    def __init__(self, placeholder: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText(placeholder)
        layout.addWidget(self._edit)

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse)
        layout.addWidget(browse_btn)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", self._edit.text())
        if folder:
            self._edit.setText(folder)

    @property
    def path(self) -> str:
        return self._edit.text().strip()

    @path.setter
    def path(self, value: str) -> None:
        self._edit.setText(value)


class SettingsDialog(QDialog):
    """Modal dialog for configuring MusicFlow folder paths and companion rules."""

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings — MusicFlow")
        self.setMinimumWidth(580)
        self._config = config
        self._setup_ui()
        self._load_from_config()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ── Source folders ─────────────────────────────────────────────────
        src_group = QGroupBox("Source Folders")
        src_form = QFormLayout(src_group)
        self._torrent_row = _FolderRow("e.g. C:\\Downloads\\Torrents")
        self._soulseek_row = _FolderRow("e.g. C:\\Downloads\\Soulseek")
        src_form.addRow("Torrent source:", self._torrent_row)
        src_form.addRow("Soulseek source:", self._soulseek_row)
        layout.addWidget(src_group)

        # ── Staging folders ────────────────────────────────────────────────
        stg_group = QGroupBox("Staging Folders")
        stg_form = QFormLayout(stg_group)
        self._staging1_row = _FolderRow("e.g. C:\\Music\\Staging1 (analysis)")
        self._staging2_row = _FolderRow("e.g. C:\\Music\\Staging2 (ready for Picard)")
        stg_form.addRow("Staging 1 (analysis):", self._staging1_row)
        stg_form.addRow("Staging 2 (Picard queue):", self._staging2_row)
        layout.addWidget(stg_group)

        # ── Companion files ────────────────────────────────────────────────
        companion_group = QGroupBox("Companion Files")
        companion_layout = QVBoxLayout(companion_group)

        companion_note = QLabel(
            "These file extensions are copied alongside audio files during ingest\n"
            "and export (cover art, booklets, lyrics, videos, etc.).\n"
            "Enter one extension per line, including the leading dot."
        )
        companion_note.setWordWrap(True)
        companion_layout.addWidget(companion_note)

        self._companion_ext_edit = QPlainTextEdit()
        self._companion_ext_edit.setPlaceholderText(".jpg\n.png\n.nfo\n.cue\n.pdf\n...")
        self._companion_ext_edit.setMaximumHeight(120)
        companion_layout.addWidget(self._companion_ext_edit)

        layout.addWidget(companion_group)

        # ── Extension filtering ────────────────────────────────────────────────
        ext_group = QGroupBox("Extension Filters (Ingest)")
        ext_layout = QVBoxLayout(ext_group)

        ext_note = QLabel(
            "Extensions to <b>select</b> (checked by default on scan) and "
            "<b>deselect</b> (unchecked by default on scan).\n"
            "Enter one extension per line, including the leading dot."
        )
        ext_note.setWordWrap(True)
        ext_layout.addWidget(ext_note)

        ext_row = QHBoxLayout()

        select_col = QVBoxLayout()
        select_col.addWidget(QLabel("Auto-select extensions:"))
        self._select_ext_edit = QPlainTextEdit()
        self._select_ext_edit.setPlaceholderText(".flac\n.wav\n.aiff")
        self._select_ext_edit.setMaximumHeight(100)
        select_col.addWidget(self._select_ext_edit)
        ext_row.addLayout(select_col)

        deselect_col = QVBoxLayout()
        deselect_col.addWidget(QLabel("Auto-deselect extensions:"))
        self._deselect_ext_edit = QPlainTextEdit()
        self._deselect_ext_edit.setPlaceholderText(".nfo\n.log\n.cue\n.rar")
        self._deselect_ext_edit.setMaximumHeight(100)
        deselect_col.addWidget(self._deselect_ext_edit)
        ext_row.addLayout(deselect_col)

        ext_layout.addLayout(ext_row)
        layout.addWidget(ext_group)

        # ── Hi-Res analysis ─────────────────────────────────────────────
        hires_group = QGroupBox("Hi-Res Analysis")
        hires_form = QFormLayout(hires_group)
        self._cutoff_ratio_edit = QLineEdit()
        self._cutoff_ratio_edit.setPlaceholderText("0.50")
        self._slope_threshold_edit = QLineEdit()
        self._slope_threshold_edit.setPlaceholderText("80.0")
        hires_form.addRow("Cutoff ratio threshold:", self._cutoff_ratio_edit)
        hires_form.addRow("Brick-wall slope threshold (dB/oct):", self._slope_threshold_edit)
        layout.addWidget(hires_group)

        # ── Note ───────────────────────────────────────────────────────────
        note = QLabel(
            "<i>Torrent source files are <b>copied</b> (originals kept for seeding).<br>"
            "Soulseek source files are <b>moved</b> (originals deleted after ingest).</i>"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        # ── Buttons ────────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_from_config(self) -> None:
        self._torrent_row.path = self._config.torrent_source_folder
        self._soulseek_row.path = self._config.soulseek_source_folder
        self._staging1_row.path = self._config.staging_folder_1
        self._staging2_row.path = self._config.staging_folder_2
        self._companion_ext_edit.setPlainText(
            "\n".join(self._config.companion_extensions)
        )
        self._select_ext_edit.setPlainText("\n".join(self._config.select_extensions))
        self._deselect_ext_edit.setPlainText("\n".join(self._config.deselect_extensions))
        self._cutoff_ratio_edit.setText(str(self._config.fake_hires_cutoff_ratio_threshold))
        self._slope_threshold_edit.setText(str(self._config.fake_hires_slope_threshold_db_oct))

    def _save(self) -> None:
        self._config.torrent_source_folder = self._torrent_row.path
        self._config.soulseek_source_folder = self._soulseek_row.path
        self._config.staging_folder_1 = self._staging1_row.path
        self._config.staging_folder_2 = self._staging2_row.path

        # Parse extensions helper
        def _parse_extensions(text: str) -> list[str]:
            return [
                line.strip().lower()
                for line in text.splitlines()
                if line.strip().startswith(".")
            ]

        # Parse companion extensions
        raw = self._companion_ext_edit.toPlainText()
        self._config.companion_extensions = _parse_extensions(raw)

        # Parse select/deselect extensions
        self._config.select_extensions = _parse_extensions(
            self._select_ext_edit.toPlainText()
        )
        self._config.deselect_extensions = _parse_extensions(
            self._deselect_ext_edit.toPlainText()
        )

        try:
            self._config.fake_hires_cutoff_ratio_threshold = float(
                self._cutoff_ratio_edit.text().strip()
            )
        except ValueError:
            self._config.fake_hires_cutoff_ratio_threshold = 0.50

        try:
            self._config.fake_hires_slope_threshold_db_oct = float(
                self._slope_threshold_edit.text().strip()
            )
        except ValueError:
            self._config.fake_hires_slope_threshold_db_oct = 80.0

        warnings = self._config.validate()
        if warnings:
            msg = "Configuration saved with warnings:\n\n" + "\n".join(f"• {w}" for w in warnings)
            QMessageBox.warning(self, "Settings Warning", msg)

        self.accept()

    def get_config(self) -> AppConfig:
        return self._config
