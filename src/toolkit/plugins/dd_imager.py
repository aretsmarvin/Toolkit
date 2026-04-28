"""Disk imaging plugin using `dd`.

Creates a bit-for-bit copy of a source device to a destination using dd.
The entire operation runs in a QThread so the GUI never freezes.
"""
from __future__ import annotations

import logging
import shlex
import subprocess

from PySide6 import QtCore, QtWidgets

from . import BasePlugin, PluginMeta


LOGGER = logging.getLogger(__name__)


class DDWorker(QtCore.QObject):
    """Run `dd` in a background thread, streaming progress back via signals."""

    started = QtCore.Signal()
    finished = QtCore.Signal(int)   # exit code
    progress = QtCore.Signal(str)   # one line of dd output
    error = QtCore.Signal(str)      # human-readable error message

    def __init__(
        self,
        source: str,
        destination: str,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._source = source
        self._destination = destination

    @QtCore.Slot()
    def run(self) -> None:
        self.started.emit()

        # `status=progress` prints periodic throughput statistics to stderr;
        # we merge stderr into stdout so the single reader loop sees everything.
        cmd = [
            "dd",
            f"if={self._source}",
            f"of={self._destination}",
            "bs=1m",
            "status=progress",
        ]
        LOGGER.info("Executing: %s", " ".join(shlex.quote(c) for c in cmd))

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            msg = "`dd` not found on this system."
            LOGGER.error(msg)
            self.error.emit(msg)
            self.finished.emit(1)
            return
        except Exception as exc:
            msg = f"Failed to start dd: {exc}"
            LOGGER.exception(msg)
            self.error.emit(msg)
            self.finished.emit(1)
            return

        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if line:
                self.progress.emit(line)

        rc = process.wait()
        self.finished.emit(rc)


class DDImagerWidget(QtWidgets.QWidget):
    """Self-contained UI widget for the dd imaging plugin."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Warning banner
        warning = QtWidgets.QLabel(
            "<b>\u26a0\ufe0f Warning:</b> The destination will be "
            "<b>completely overwritten</b>. Make sure the destination "
            "device or file is empty and you have a backup of any data you "
            "want to keep."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "background: #5a1a1a; color: #ffcccc;"
            "border-radius: 6px; padding: 10px;"
        )
        root.addWidget(warning)

        # Source selection
        source_group = QtWidgets.QGroupBox("Source")
        source_layout = QtWidgets.QHBoxLayout(source_group)
        self.source_edit = QtWidgets.QLineEdit()
        self.source_edit.setPlaceholderText("/dev/disk2  (e.g. the drive to image)")
        source_browse = QtWidgets.QPushButton("Browse\u2026")
        source_browse.clicked.connect(self._browse_source)
        source_layout.addWidget(self.source_edit)
        source_layout.addWidget(source_browse)
        root.addWidget(source_group)

        # Destination selection
        dest_group = QtWidgets.QGroupBox("Destination")
        dest_layout = QtWidgets.QHBoxLayout(dest_group)
        self.dest_edit = QtWidgets.QLineEdit()
        self.dest_edit.setPlaceholderText("/path/to/image.dd  (empty file or device)")
        dest_browse = QtWidgets.QPushButton("Browse\u2026")
        dest_browse.clicked.connect(self._browse_destination)
        dest_layout.addWidget(self.dest_edit)
        dest_layout.addWidget(dest_browse)
        root.addWidget(dest_group)

        # Progress and log output
        output_label = QtWidgets.QLabel("Output / Progress:")
        root.addWidget(output_label)

        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(220)
        self.output.setPlaceholderText("dd output will appear here once imaging starts\u2026")
        root.addWidget(self.output, stretch=1)

        # Status bar inside the plugin
        self.status_label = QtWidgets.QLabel("Ready.")
        self.status_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        root.addWidget(self.status_label)

        # Action buttons
        btn_row = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("\u25b6  Start imaging")
        self.start_button.clicked.connect(self._start)
        self.clear_button = QtWidgets.QPushButton("Clear log")
        self.clear_button.clicked.connect(self.output.clear)
        btn_row.addStretch(1)
        btn_row.addWidget(self.clear_button)
        btn_row.addWidget(self.start_button)
        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @QtCore.Slot()
    def _browse_source(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select source device or file",
            "/dev",
            "All files (*)",
        )
        if path:
            self.source_edit.setText(path)

    @QtCore.Slot()
    def _browse_destination(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Select destination file",
            "",
            "Disk images (*.dd *.img *.raw);;All files (*)",
        )
        if path:
            self.dest_edit.setText(path)

    @QtCore.Slot()
    def _start(self) -> None:
        source = self.source_edit.text().strip()
        dest = self.dest_edit.text().strip()

        if not source or not dest:
            QtWidgets.QMessageBox.warning(
                self,
                "Missing information",
                "Please provide both a source device/file and a destination path.",
            )
            return

        confirm = QtWidgets.QMessageBox.question(
            self,
            "Confirm destructive operation",
            (
                f"This will copy:\n\n"
                f"  Source:      {source}\n"
                f"  Destination: {dest}\n\n"
                "All data on the destination will be overwritten.\n"
                "Make sure the destination is empty before proceeding.\n\n"
                "Continue?"
            ),
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.start_button.setEnabled(False)
        self.output.clear()
        self.status_label.setText("Imaging in progress\u2026")
        LOGGER.info("Starting dd imaging: %s -> %s", source, dest)

        self._thread = QtCore.QThread(self)
        self._worker = DDWorker(source, dest)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)

        self._thread.start()

    @QtCore.Slot(str)
    def _on_progress(self, line: str) -> None:
        self.output.appendPlainText(line)
        self.status_label.setText(line)

    @QtCore.Slot(str)
    def _on_error(self, message: str) -> None:
        LOGGER.error("dd error: %s", message)
        self.output.appendPlainText(f"[ERROR] {message}")
        QtWidgets.QMessageBox.critical(self, "dd error", message)

    @QtCore.Slot(int)
    def _on_finished(self, rc: int) -> None:
        self.start_button.setEnabled(True)
        if rc == 0:
            self.status_label.setText("Imaging complete.")
            LOGGER.info("dd imaging completed successfully")
            QtWidgets.QMessageBox.information(self, "Imaging complete", "dd finished successfully.")
        else:
            self.status_label.setText(f"dd exited with code {rc}.")
            LOGGER.warning("dd imaging failed with exit code %d", rc)
            QtWidgets.QMessageBox.warning(self, "Imaging failed", f"dd exited with code {rc}.")


class Plugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(
            PluginMeta(
                name="Disk Imaging (dd)",
                description="Create a bit-for-bit copy of a source drive to a destination file or device using dd.",
                category="Acquisition",
            ),
        )

    def create_ui(self, parent: QtWidgets.QWidget) -> None:
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        widget = DDImagerWidget(parent)
        layout.addWidget(widget)
