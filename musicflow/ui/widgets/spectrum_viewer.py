"""Spectrum viewer widget for displaying spectrogram results."""

from __future__ import annotations

from io import BytesIO

import numpy as np
from matplotlib.figure import Figure
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from musicflow.core.fake_hires import SpectrumResult


class _SpectrumRenderWorker(QThread):
    rendered = Signal(object)

    def __init__(self, result: SpectrumResult, size: tuple[int, int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._result = result
        self._size = size

    def run(self) -> None:
        fig = Figure(figsize=(max(self._size[0], 1) / 200.0, max(self._size[1], 1) / 200.0), dpi=200)
        fig.patch.set_facecolor("#1e1e2e")
        ax = fig.add_subplot(111)
        ax.set_facecolor("#1e1e2e")

        result = self._result
        if result.spectrogram_db.ndim != 2 or result.spectrogram_db.size == 0:
            ax.text(0.5, 0.5, f"No spectrogram data\n{result.reason}", transform=ax.transAxes,
                    ha="center", va="center", color="#f38ba8", fontsize=10)
        else:
            times = result.spectrogram_times
            freqs_khz = result.spectrogram_freqs / 1000.0
            db_clipped = np.clip(result.spectrogram_db, -80.0, 0.0)
            img = ax.imshow(
                db_clipped,
                aspect="auto",
                origin="lower",
                extent=[float(times[0]), float(times[-1]), float(freqs_khz[0]), float(freqs_khz[-1])],
                cmap="inferno",
                vmin=-80.0,
                vmax=0.0,
                interpolation="nearest",
            )
            nyquist_khz = result.nyquist_hz / 1000.0
            cutoff_khz = result.actual_cutoff_hz / 1000.0
            ax.axhline(nyquist_khz, color="#45475a", linewidth=1, linestyle="--")
            cutoff_color = "#f38ba8" if result.is_suspect else "#a6e3a1"
            ax.axhline(cutoff_khz, color=cutoff_color, linewidth=1.5, linestyle="-")
            ax.set_xlabel("Time (s)", fontsize=8)
            ax.set_ylabel("Frequency (kHz)", fontsize=8)
            title = result.path.name
            if result.hi_res_verdict is not None:
                title += f" [{result.hi_res_verdict.label}]"
            elif result.is_suspect:
                title += " [SUSPECT]"
            ax.set_title(title, fontsize=8, color="#cdd6f4")
            fig.colorbar(img, ax=ax, label="dBFS", pad=0.02)

            if result.hi_res_verdict is not None and result.hi_res_verdict.evidence:
                evidence = "\n".join(f"• {item.interpretation}" for item in result.hi_res_verdict.evidence[:3])
                fig.text(0.01, 0.01, evidence, fontsize=6, color="#a6adc8", va="bottom", ha="left")

        buf = BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        pixmap = QPixmap()
        pixmap.loadFromData(buf.getvalue())
        self.rendered.emit(pixmap)


class SpectrumViewer(QWidget):
    """Widget that renders a spectrogram image without blocking the UI."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_result: SpectrumResult | None = None
        self._render_worker: _SpectrumRenderWorker | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel("Select a file to view its spectrum")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._label.setStyleSheet("color: #585b70; font-size: 10pt;")
        layout.addWidget(self._label)

    def clear(self) -> None:
        self._current_result = None
        if self._render_worker is not None and self._render_worker.isRunning():
            self._render_worker.requestInterruption()
        self._render_worker = None
        self._label.setText("Select a file to view its spectrum")
        self._label.setPixmap(QPixmap())

    def show_result(self, result: SpectrumResult) -> None:
        self._current_result = result
        if self._render_worker is not None and self._render_worker.isRunning():
            self._render_worker.requestInterruption()
        size = (2000, 1000)
        worker = _SpectrumRenderWorker(result, size, self)
        worker.rendered.connect(self._on_rendered)
        self._render_worker = worker
        self._label.setText("Rendering…")
        worker.start()

    def _on_rendered(self, pixmap: object) -> None:
        if not isinstance(pixmap, QPixmap):
            return
        if self._current_result is None:
            return
        scaled = pixmap.scaled(
            self._label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
