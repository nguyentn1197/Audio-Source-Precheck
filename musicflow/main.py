"""MusicFlow entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from musicflow.ui.main_window import MainWindow
from musicflow.utils.logging_utils import configure_logging


def main() -> None:
    configure_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("MusicFlow")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("MusicFlow")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
