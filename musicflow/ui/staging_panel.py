"""Staging panel — Ingest tab.

Shows source folder trees for Torrent and Soulseek with:
  - Per-item checkboxes (folder nodes use auto-tristate; all checked by default).
  - Select All / Deselect All buttons per tree.
  - Right-click context menu → Open in File Explorer.
  - Only checked items are passed to the ingest pipeline.
  - Companion files (cover art, booklets, etc.) are shown in the tree and
    carried into staging-1 alongside audio.
  - Background scanning to keep UI responsive.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
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
from musicflow.core.ingest import (
    IngestResult,
    IngestWorker,
    ScanWorker,
    SourceItem,
    SourceType,
    _MultiSourceIngestAnalysisWorker,
    _cleanup_empty_folders,
    ingest_files,
    scan_source,
)
from musicflow.utils.file_utils import AUDIO_EXTENSIONS, fmt_size, open_in_explorer
from musicflow.utils.logging_utils import get_logger

# Re-export for backward compatibility during transition
_fmt_size = fmt_size

logger = get_logger(__name__)

# Qt item flags used on every checkable node
_FLAG_FOLDER = (
    Qt.ItemFlag.ItemIsUserCheckable
    | Qt.ItemFlag.ItemIsEnabled
    | Qt.ItemFlag.ItemIsAutoTristate
)
_FLAG_FILE = Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled

# Column indices
_COL_NAME = 0
_COL_SIZE = 1
_COL_TYPE = 2

# Item data roles
_COL_IS_FILE_ROLE = Qt.ItemDataRole.UserRole + 1  # bool: True = file, False = folder


class StagingPanel(QWidget):
    """Ingest tab: scan sources → ingest → view staging-1."""

    ingest_completed = Signal(object)     # IngestResult
    ingest_worker_ready = Signal(object)  # _MultiSourceIngestAnalysisWorker (emitted before start)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._torrent_items: list[SourceItem] = []
        self._soulseek_items: list[SourceItem] = []
        self._worker: IngestWorker | None = None
        self._scan_worker: ScanWorker | None = None
        self._torrent_path_for_scan: Path | None = None
        self._soulseek_path_for_scan: Path | None = None
        self._setup_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Top toolbar
        toolbar = QHBoxLayout()
        self._scan_btn = QPushButton("Scan Source Folders")
        self._scan_btn.clicked.connect(self._scan)
        toolbar.addWidget(self._scan_btn)

        self._ingest_btn = QPushButton("Ingest Selected to Staging 1")
        self._ingest_btn.setEnabled(False)
        self._ingest_btn.clicked.connect(self._ingest)
        toolbar.addWidget(self._ingest_btn)

        toolbar.addStretch()
        self._status_label = QLabel("Ready")
        toolbar.addWidget(self._status_label)
        layout.addLayout(toolbar)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # Source trees in a horizontal splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_source_group(
            title="Torrent Source (copy — originals kept)",
            tree_attr="_torrent_tree",
        ))
        splitter.addWidget(self._build_source_group(
            title="Soulseek Source (move — originals deleted)",
            tree_attr="_slsk_tree",
        ))
        layout.addWidget(splitter, stretch=1)

        # Staging-1 summary
        self._staging_label = QLabel("Staging 1: not yet ingested")
        layout.addWidget(self._staging_label)

    def _build_source_group(self, title: str, tree_attr: str) -> QGroupBox:
        """Build a QGroupBox containing Select All / Deselect All + a QTreeWidget."""
        group = QGroupBox(title)
        group_layout = QVBoxLayout(group)

        # Select / Deselect row
        btn_row = QHBoxLayout()
        sel_all_btn = QPushButton("Select All")
        desel_all_btn = QPushButton("Deselect All")
        btn_row.addWidget(sel_all_btn)
        btn_row.addWidget(desel_all_btn)
        sel_exts_btn = QPushButton("Select by Extension")
        desel_exts_btn = QPushButton("Deselect by Extension")
        sel_exts_btn.clicked.connect(lambda: self._select_by_extension(tree, True))
        desel_exts_btn.clicked.connect(lambda: self._select_by_extension(tree, False))
        btn_row.addWidget(sel_exts_btn)
        btn_row.addWidget(desel_exts_btn)
        btn_row.addStretch()
        group_layout.addLayout(btn_row)

        # Tree widget
        tree = QTreeWidget()
        tree.setHeaderLabels(["Name", "Size", "Type"])
        tree.setColumnWidth(_COL_NAME, 280)
        tree.setColumnWidth(_COL_SIZE, 70)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(
            lambda pos, t=tree: self._show_tree_context_menu(t, pos)
        )
        group_layout.addWidget(tree)

        # Wire buttons
        sel_all_btn.clicked.connect(lambda: _set_all_checked(tree, True))
        desel_all_btn.clicked.connect(lambda: _set_all_checked(tree, False))

        # Store tree as instance attribute
        setattr(self, tree_attr, tree)
        return group

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh_config(self, config: AppConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def _scan(self) -> None:
        warnings = self._config.validate()
        if warnings:
            QMessageBox.warning(
                self,
                "Configuration Incomplete",
                "Please configure all folders in Settings first.\n\n"
                + "\n".join(f"• {w}" for w in warnings),
            )
            return

        # Disable scan button and show progress
        self._scan_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._progress.setMaximum(2)
        self._status_label.setText("Scanning...")
        self._status_label.setStyleSheet("")

        # Clear trees
        self._torrent_items = []
        self._soulseek_items = []
        self._torrent_tree.clear()
        self._slsk_tree.clear()

        # Store paths for the worker
        self._torrent_path_for_scan = self._config.torrent_path()
        self._soulseek_path_for_scan = self._config.soulseek_path()

        # Create and start scan worker (background thread)
        self._scan_worker = ScanWorker(
            self._torrent_path_for_scan,
            self._soulseek_path_for_scan,
            self._config.companion_extensions,
        )
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.item_found.connect(self._on_item_found)
        self._scan_worker.archive_progress.connect(self._on_archive_progress)
        self._scan_worker.finished.connect(self._on_scan_done)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_worker.start()

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def _ingest(self) -> None:
        staging1 = self._config.staging1_path()
        if staging1 is None:
            QMessageBox.warning(self, "Error", "Staging 1 folder is not configured.")
            return

        # Filter by checked items
        torrent_checked = _checked_paths(self._torrent_tree)
        soulseek_checked = _checked_paths(self._slsk_tree)

        torrent_to_ingest = [i for i in self._torrent_items if i.path in torrent_checked]
        soulseek_to_ingest = [i for i in self._soulseek_items if i.path in soulseek_checked]

        if not torrent_to_ingest and not soulseek_to_ingest:
            QMessageBox.information(
                self,
                "Nothing selected",
                "Check at least one file in the source trees before ingesting.",
            )
            return

        self._ingest_btn.setEnabled(False)
        self._scan_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setValue(0)

        torrent_root = self._config.torrent_path() or Path(".")
        soulseek_root = self._config.soulseek_path() or Path(".")

        self._worker = _MultiSourceIngestAnalysisWorker(
            torrent_items=torrent_to_ingest,
            soulseek_items=soulseek_to_ingest,
            torrent_root=torrent_root,
            soulseek_root=soulseek_root,
            staging=staging1,
            config=self._config,
            companion_extensions=self._config.companion_extensions,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_ingest_done)
        self._worker.error.connect(self._on_error)
        # Emit before start so MainWindow can wire file_ready before any signals fire
        self.ingest_worker_ready.emit(self._worker)
        self._worker.start()

    # ------------------------------------------------------------------
    # Slots — Scanning
    # ------------------------------------------------------------------

    def _on_scan_progress(self, current: int, total: int, folder_path: str) -> None:
        """Update progress during scanning."""
        if total > 0:
            self._progress.setMaximum(total)
            self._progress.setValue(current)
        self._status_label.setText(f"Scanning: {folder_path}")

    def _on_item_found(self, item: SourceItem, source_type: str) -> None:
        """Add a single item to the tree as it is discovered."""
        if source_type == "torrent":
            self._torrent_items.append(item)
            tree = self._torrent_tree
            root = self._torrent_path_for_scan
        else:
            self._soulseek_items.append(item)
            tree = self._slsk_tree
            root = self._soulseek_path_for_scan

        if root:
            _populate_tree_incremental(tree, [item], root)

    def _on_archive_progress(self, archive_name: str, current: int, total: int, source_type: str) -> None:
        """Update progress during archive extraction."""
        if total > 0:
            self._progress.setMaximum(total)
            self._progress.setValue(current)
        self._status_label.setText(f"Extracting [{current}/{total}]: {archive_name}")

    def _on_scan_done(self, torrent_items: list[SourceItem], soulseek_items: list[SourceItem]) -> None:
        """Final status update (trees already populated incrementally via _on_scan_batch)."""
        # Update status
        audio_total = sum(
            1 for i in self._torrent_items + self._soulseek_items
            if not i.is_archive and not i.is_companion
        )
        companion_total = sum(
            1 for i in self._torrent_items + self._soulseek_items
            if i.is_companion
        )

        self._progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._status_label.setText(
            f"✓ Found {audio_total} audio, {companion_total} companion files"
        )
        self._status_label.setStyleSheet("color: #5cb85c;")
        self._ingest_btn.setEnabled(audio_total > 0)

    def _on_scan_error(self, msg: str) -> None:
        """Handle scan error."""
        self._progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._status_label.setText("⚠ Scan error")
        self._status_label.setStyleSheet("color: #d9534f;")
        QMessageBox.critical(self, "Scan Error", msg)

    # ------------------------------------------------------------------
    # Slots — Ingestion
    # ------------------------------------------------------------------

    def _on_progress(self, current: int, total: int, filename: str) -> None:
        if total > 0:
            self._progress.setMaximum(total)
            self._progress.setValue(current)
        self._status_label.setText(f"Ingesting: {filename}")
        self._status_label.setStyleSheet("")

    def _on_ingest_done(self, result: IngestResult) -> None:
        self._progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._ingest_btn.setEnabled(True)
        
        # Build summary
        summary = (
            f"Staging 1: {result.success_count} audio"
            f" + {result.companion_count} companion files ingested"
        )
        if result.error_count:
            summary += f" ({result.error_count} errors)"
        self._staging_label.setText(summary)
        
        # Update status label
        if result.error_count == 0:
            self._status_label.setText(
                f"✓ Done — {result.success_count} audio files in Staging 1"
            )
            self._status_label.setStyleSheet("color: #5cb85c; font-weight: bold;")
        else:
            self._status_label.setText(
                f"⚠ Done with {result.error_count} error(s) — {result.success_count} files ingested"
            )
            self._status_label.setStyleSheet("color: #d9534f; font-weight: bold;")
        
        # Show error details if any
        if result.error_count:
            error_details = "\n".join(
                f"• {p.name}:\n  {e}" for p, e in result.errors[:10]
            )
            if len(result.errors) > 10:
                error_details += f"\n... and {len(result.errors) - 10} more errors"
            
            QMessageBox.warning(
                self,
                "Ingest Errors",
                f"{result.error_count} file(s) failed to ingest:\n\n{error_details}",
            )
        self.ingest_completed.emit(result)

    def _on_error(self, msg: str) -> None:
        self._progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._ingest_btn.setEnabled(True)
        QMessageBox.critical(self, "Ingest Error", msg)

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------

    def _show_tree_context_menu(self, tree: QTreeWidget, pos: QPoint) -> None:
        item = tree.itemAt(pos)
        if item is None:
            return
        path: Path | None = item.data(_COL_NAME, Qt.ItemDataRole.UserRole)
        if path is None:
            return

        menu = QMenu(self)
        action = menu.addAction("Open in File Explorer")
        action.triggered.connect(lambda: open_in_explorer(path))

        # "Rescan this folder" for folders only
        if path.is_dir():
            menu.addSeparator()
            rescan_action = menu.addAction("Rescan this folder")
            source_type = "torrent" if tree == self._torrent_tree else "soulseek"
            rescan_action.triggered.connect(
                lambda: self._rescan_folder(path, source_type)
            )

        menu.exec(tree.viewport().mapToGlobal(pos))

    def _rescan_folder(self, folder_path: Path, source_type: str) -> None:
        """Rescan a single folder and update its tree items."""
        if source_type == "torrent":
            tree = self._torrent_tree
            items_list = self._torrent_items
            root = self._torrent_path_for_scan
        else:
            tree = self._slsk_tree
            items_list = self._soulseek_items
            root = self._soulseek_path_for_scan

        if root is None:
            return

        try:
            # Scan just this folder
            source_enum = (
                SourceType.TORRENT
                if source_type == "torrent"
                else SourceType.SOULSEEK
            )
            new_items = scan_source(
                folder_path,
                source_enum,
                companion_extensions=self._config.companion_extensions,
            )

            # Remove old items from this folder
            items_list[:] = [
                i for i in items_list if not str(i.path).startswith(str(folder_path))
            ]

            # Add new items
            items_list.extend(new_items)

            # Remove folder node from tree
            _remove_tree_folder(tree, folder_path)

            # Repopulate tree with new items
            _populate_tree_incremental(tree, new_items, root)

            self._status_label.setText(f"✓ Rescanned {folder_path.name}")
            self._status_label.setStyleSheet("color: #5cb85c;")
        except Exception as exc:
            logger.exception("Folder rescan failed")
            self._status_label.setText("⚠ Rescan failed")
            self._status_label.setStyleSheet("color: #d9534f;")
            QMessageBox.critical(
                self,
                "Rescan Error",
                f"Failed to rescan {folder_path.name}:\n\n{exc}",
            )

    def _select_by_extension(self, tree: QTreeWidget, select: bool) -> None:
        """Select/deselect files by extension based on config. Never touches filesystem."""
        extensions = (
            self._config.select_extensions if select
            else self._config.deselect_extensions
        )

        def _walk(item: QTreeWidgetItem) -> None:
            is_file: bool = item.data(_COL_NAME, _COL_IS_FILE_ROLE) or False
            if is_file:
                path: Path | None = item.data(_COL_NAME, Qt.ItemDataRole.UserRole)
                if path is not None and path.suffix.lower() in extensions:
                    state = Qt.CheckState.Checked if select else Qt.CheckState.Unchecked
                    item.setCheckState(_COL_NAME, state)
            for i in range(item.childCount()):
                _walk(item.child(i))

        root = tree.invisibleRootItem()
        for i in range(root.childCount()):
            _walk(root.child(i))


# ---------------------------------------------------------------------------
# Module-level tree helpers
# ---------------------------------------------------------------------------


def _populate_tree(tree: QTreeWidget, items: list[SourceItem], root: Path) -> None:
    """Fill *tree* with checkable SourceItem nodes grouped by subfolder.

    Folder nodes use ItemIsAutoTristate so checking/unchecking a folder
    cascades to all its children automatically.  All nodes default to Checked.
    Each node stores its filesystem Path in UserRole for context menus and
    checkbox filtering.
    """
    tree.clear()
    tree.blockSignals(True)  # prevent cascading tristate updates during population

    folder_nodes: dict[Path, QTreeWidgetItem] = {}

    for item in items:
        try:
            rel = item.path.relative_to(root)
            parent_rel = rel.parent
        except ValueError:
            parent_rel = Path(".")

        # Ensure every ancestor folder node exists (top-down)
        if parent_rel != Path("."):
            parts = list(parent_rel.parts)
            for depth in range(1, len(parts) + 1):
                ancestor_rel = Path(*parts[:depth])
                if ancestor_rel not in folder_nodes:
                    ancestor_path = root / ancestor_rel
                    node = QTreeWidgetItem([ancestor_rel.name, "", "Folder"])
                    node.setFlags(_FLAG_FOLDER)
                    node.setCheckState(_COL_NAME, Qt.CheckState.Checked)
                    node.setData(_COL_NAME, Qt.ItemDataRole.UserRole, ancestor_path)
                    node.setData(_COL_NAME, _COL_IS_FILE_ROLE, False)

                    if depth > 1:
                        parent_key = Path(*parts[:depth - 1])
                        folder_nodes[parent_key].addChild(node)
                    else:
                        tree.addTopLevelItem(node)
                    folder_nodes[ancestor_rel] = node

            parent_node: QTreeWidgetItem = folder_nodes[parent_rel]
        else:
            parent_node = tree.invisibleRootItem()

        # File node
        size_str = _fmt_size(item.size) if item.size is not None else ""
        if item.is_archive:
            type_str = "Archive"
        elif item.is_companion:
            type_str = "Companion"
        else:
            type_str = "Audio"

        child = QTreeWidgetItem([item.path.name, size_str, type_str])
        child.setFlags(_FLAG_FILE)
        child.setCheckState(_COL_NAME, Qt.CheckState.Checked)
        child.setData(_COL_NAME, Qt.ItemDataRole.UserRole, item.path)
        child.setData(_COL_NAME, _COL_IS_FILE_ROLE, True)
        parent_node.addChild(child)

    tree.blockSignals(False)


def _set_all_checked(tree: QTreeWidget, checked: bool) -> None:
    """Set every node in *tree* to Checked or Unchecked."""
    state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked

    def _walk(item: QTreeWidgetItem) -> None:
        item.setCheckState(_COL_NAME, state)
        for i in range(item.childCount()):
            _walk(item.child(i))

    root = tree.invisibleRootItem()
    for i in range(root.childCount()):
        _walk(root.child(i))


def _checked_paths(tree: QTreeWidget) -> set[Path]:
    """Return the set of file Paths whose tree item is Checked or PartiallyChecked."""
    result: set[Path] = set()

    def _walk(item: QTreeWidgetItem) -> None:
        is_file: bool = item.data(_COL_NAME, _COL_IS_FILE_ROLE) or False
        if is_file and item.checkState(_COL_NAME) != Qt.CheckState.Unchecked:
            path: Path | None = item.data(_COL_NAME, Qt.ItemDataRole.UserRole)
            if path is not None:
                result.add(path)
        for i in range(item.childCount()):
            _walk(item.child(i))

    root = tree.invisibleRootItem()
    for i in range(root.childCount()):
        _walk(root.child(i))
    return result


def _collect_existing_folders(
    item: QTreeWidgetItem,
    root: Path,
    folder_nodes: dict[Path, QTreeWidgetItem],
) -> None:
    """Recursively collect all folder nodes already in tree.
    
    Stores folder paths relative to root in folder_nodes dict for quick lookup.
    """
    path: Path | None = item.data(_COL_NAME, Qt.ItemDataRole.UserRole)
    if path is not None:
        try:
            rel = path.relative_to(root)
            is_file: bool = item.data(_COL_NAME, _COL_IS_FILE_ROLE) or False
            if not is_file:
                folder_nodes[rel] = item
        except ValueError:
            pass

    for i in range(item.childCount()):
        _collect_existing_folders(item.child(i), root, folder_nodes)


def _populate_tree_incremental(
    tree: QTreeWidget,
    new_items: list[SourceItem],
    root: Path,
) -> None:
    """Add new items to tree without rebuilding entire tree.
    
    Only creates new nodes for items not already in the tree.
    Does NOT call expandAll() to avoid expensive re-layout during scanning.
    Defers expandAll() to the final _on_scan_done() call.
    """
    tree.blockSignals(True)

    # Collect existing folder nodes to avoid recreating them
    folder_nodes: dict[Path, QTreeWidgetItem] = {}
    _collect_existing_folders(tree.invisibleRootItem(), root, folder_nodes)

    for item in new_items:
        try:
            rel = item.path.relative_to(root)
            parent_rel = rel.parent
        except ValueError:
            parent_rel = Path(".")

        # Ensure every ancestor folder node exists (top-down)
        if parent_rel != Path("."):
            parts = list(parent_rel.parts)
            for depth in range(1, len(parts) + 1):
                ancestor_rel = Path(*parts[:depth])
                if ancestor_rel not in folder_nodes:
                    ancestor_path = root / ancestor_rel
                    node = QTreeWidgetItem([ancestor_rel.name, "", "Folder"])
                    node.setFlags(_FLAG_FOLDER)
                    node.setCheckState(_COL_NAME, Qt.CheckState.Checked)
                    node.setData(_COL_NAME, Qt.ItemDataRole.UserRole, ancestor_path)
                    node.setData(_COL_NAME, _COL_IS_FILE_ROLE, False)

                    if depth > 1:
                        parent_key = Path(*parts[:depth - 1])
                        folder_nodes[parent_key].addChild(node)
                    else:
                        tree.addTopLevelItem(node)
                    folder_nodes[ancestor_rel] = node

            parent_node: QTreeWidgetItem = folder_nodes[parent_rel]
        else:
            parent_node = tree.invisibleRootItem()

        # File node
        size_str = _fmt_size(item.size) if item.size is not None else ""
        if item.is_archive:
            type_str = "Archive"
        elif item.is_companion:
            type_str = "Companion"
        else:
            type_str = "Audio"

        child = QTreeWidgetItem([item.path.name, size_str, type_str])
        child.setFlags(_FLAG_FILE)
        child.setCheckState(_COL_NAME, Qt.CheckState.Checked)
        child.setData(_COL_NAME, Qt.ItemDataRole.UserRole, item.path)
        child.setData(_COL_NAME, _COL_IS_FILE_ROLE, True)
        parent_node.addChild(child)

    tree.blockSignals(False)


def _remove_tree_folder(tree: QTreeWidget, folder_path: Path) -> None:
    """Remove all tree items under a specific folder.
    
    Walks the tree and removes any items whose path starts with folder_path.
    """

    def _walk(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
        """Return list of children to remove."""
        to_remove = []
        path: Path | None = item.data(_COL_NAME, Qt.ItemDataRole.UserRole)
        if path and str(path).startswith(str(folder_path)):
            to_remove.append(item)
        else:
            for i in range(item.childCount()):
                to_remove.extend(_walk(item.child(i)))
        return to_remove

    root_item = tree.invisibleRootItem()
    for item in _walk(root_item):
        parent = item.parent()
        if parent:
            parent.removeChild(item)
        else:
            idx = tree.indexOfTopLevelItem(item)
            if idx >= 0:
                tree.takeTopLevelItem(idx)


# ---------------------------------------------------------------------------
# Multi-source ingest worker
# ---------------------------------------------------------------------------


class _MultiSourceIngestWorker(IngestWorker):
    """Ingests Torrent items then Soulseek items sequentially in a background thread."""

    def __init__(
        self,
        torrent_items: list[SourceItem],
        soulseek_items: list[SourceItem],
        torrent_root: Path,
        soulseek_root: Path,
        staging: Path,
        companion_extensions: list[str] | None = None,
    ) -> None:
        # Pass combined list to super (we override run so the list isn't used directly)
        super().__init__(
            torrent_items + soulseek_items,
            torrent_root,
            staging,
            companion_extensions=companion_extensions,
        )
        self._torrent_items = torrent_items
        self._soulseek_items = soulseek_items
        self._torrent_root = torrent_root
        self._soulseek_root = soulseek_root
        self._staging = staging
        self._companion_extensions = companion_extensions or []

    def run(self) -> None:  # type: ignore[override]
        log = get_logger(__name__)
        try:
            combined = IngestResult()
            total = len(self._torrent_items) + len(self._soulseek_items)
            offset = 0

            def _cb(cur: int, tot: int, name: str, is_error: bool) -> None:
                self.progress.emit(offset + cur, total, name, is_error)

            if self._torrent_items:
                r = ingest_files(
                    self._torrent_items,
                    self._torrent_root,
                    self._staging,
                    _cb,
                    companion_extensions=self._companion_extensions,
                )
                combined.staged.extend(r.staged)
                combined.errors.extend(r.errors)
                offset += len(self._torrent_items)

            if self._soulseek_items:
                r = ingest_files(
                    self._soulseek_items,
                    self._soulseek_root,
                    self._staging,
                    _cb,
                    companion_extensions=self._companion_extensions,
                )
                combined.staged.extend(r.staged)
                combined.errors.extend(r.errors)

            # Cleanup empty folders in Soulseek source after successful ingest
            if self._soulseek_items:
                deleted = _cleanup_empty_folders(self._soulseek_root)
                if deleted > 0:
                    log.info("Cleaned up %d empty folders in Soulseek", deleted)

            self.finished.emit(combined)
        except Exception as exc:
            log.exception("_MultiSourceIngestWorker crashed")
            self.error.emit(str(exc))
