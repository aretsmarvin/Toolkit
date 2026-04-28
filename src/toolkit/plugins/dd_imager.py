"""Disk imaging plugin using `dd`.

Provides a full GUI for creating bit-for-bit disk images using dd.
The user can select source and destination via:
  - Direct text input
  - A file/path browser dialog
  - A live drive picker that lists all connected block devices

The dd process runs in a QThread so the GUI never freezes.
"""
from __future__ import annotations

import logging
import re
import shlex
import subprocess

from PySide6 import QtCore, QtWidgets

from . import BasePlugin, PluginMeta


LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Drive enumeration (macOS diskutil)
# ---------------------------------------------------------------------------


def list_drives() -> list[dict]:
    """Return a list of connected drives/partitions via diskutil.

    Each entry is a dict with keys: device, name, size, removable.
    Falls back to an empty list on errors.
    """
    drives: list[dict] = []
    try:
        result = subprocess.run(
            ["diskutil", "list", "-plist"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []

        # Parse plist — use plistlib so we don't need extra deps
        import plistlib
        data = plistlib.loads(result.stdout.encode())

        all_disks = data.get("AllDisksAndPartitions", [])
        for disk in all_disks:
            dev = disk.get("DeviceIdentifier", "")
            size = disk.get("Size", 0)
            name = disk.get("VolumeName", "") or disk.get("OSInternalMedia", "")
            drives.append({
                "device": f"/dev/{dev}",
                "name": name or f"Disk ({dev})",
                "size": size,
                "removable": disk.get("RemovableMedia", False),
            })
            # Also add each partition
            for part in disk.get("Partitions", []):
                pdev = part.get("DeviceIdentifier", "")
                pname = part.get("VolumeName", "") or pdev
                psize = part.get("Size", 0)
                drives.append({
                    "device": f"/dev/{pdev}",
                    "name": pname,
                    "size": psize,
                    "removable": disk.get("RemovableMedia", False),
                })
    except Exception as exc:
        LOGGER.warning("Could not enumerate drives: %s", exc)
    return drives


def format_size(n: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# ---------------------------------------------------------------------------
# Drive picker dialog
# ---------------------------------------------------------------------------


class DrivePickerDialog(QtWidgets.QDialog):
    """Modal dialog that lists all connected drives and lets the user pick one."""

    def __init__(self, title: str = "Select drive", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(600)
        self.selected_device: str | None = None

        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel("Select a drive or partition:")
        layout.addWidget(info)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Device", "Name", "Size", "Removable"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.doubleClicked.connect(self._accept)
        layout.addWidget(self.table)

        btn_row = QtWidgets.QHBoxLayout()
        refresh_btn = QtWidgets.QPushButton("\u21bb  Refresh")
        refresh_btn.clicked.connect(self._populate)
        select_btn = QtWidgets.QPushButton("Select")
        select_btn.clicked.connect(self._accept)
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(select_btn)
        layout.addLayout(btn_row)

        self._populate()

    def _populate(self) -> None:
        self.table.setRowCount(0)
        drives = list_drives()
        for drive in drives:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(drive["device"]))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(drive["name"]))
            self.table.setItem(
                row, 2, QtWidgets.QTableWidgetItem(format_size(drive["size"]))
            )
            self.table.setItem(
                row, 3,
                QtWidgets.QTableWidgetItem("Yes" if drive["removable"] else "No")
            )

        if not drives:
            self.table.insertRow(0)
            item = QtWidgets.QTableWidgetItem("No drives found (try Refresh)")
            item.setForeground(QtWidgets.QApplication.palette().placeholderText())
            self.table.setItem(0, 0, item)

    @QtCore.Slot()
    def _accept(self) -> None:
        rows = self.table.selectedItems()
        if rows:
            self.selected_device = self.table.item(
                self.table.currentRow(), 0
            ).text()
            self.accept()
        else:
            QtWidgets.QMessageBox.warning(self, "No selection", "Please select a drive.")


# ---------------------------------------------------------------------------
# dd worker
# ---------------------------------------------------------------------------


class DDWorker(QtCore.QObject):
    """Run `dd` in a background thread, streaming progress back via signals."""

    started = QtCore.Signal()
    finished = QtCore.Signal(int)   # exit code
    progress = QtCore.Signal(str)   # one line of dd output
    error = QtCore.Signal(str)      # human-readable error message

    def __init__(self, source: str, destination: str,
                 parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._source = source
        self._destination = destination

    @QtCore.Slot()
    def run(self) -> None:
        self.started.emit()

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


# ---------------------------------------------------------------------------
# Main plugin widget
# ---------------------------------------------------------------------------


class DDImagerWidget(QtWidgets.QWidget):
    """Full dd imaging UI."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Warning banner
        warning = QtWidgets.QLabel(
            "<b>\u26a0\ufe0f Warning:</b> The destination will be "
            "<b>completely overwritten</b>. Make sure the destination "
            "device or file is empty and contains no data you want to keep."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "background: #5a1a1a; color: #ffcccc;"
            "border-radius: 6px; padding: 10px;"
        )
        root.addWidget(warning)

        # Source selection
        source_group = QtWidgets.QGroupBox("Source device")
        source_layout = QtWidgets.QHBoxLayout(source_group)
        self.source_edit = QtWidgets.QLineEdit()
        self.source_edit.setPlaceholderText("/dev/disk2  — type path or use Browse / Pick drive")
        source_browse = QtWidgets.QPushButton("Browse\u2026")
        source_browse.setToolTip("Browse for a file")
        source_browse.clicked.connect(self._browse_source)
        source_pick = QtWidgets.QPushButton("\U0001f4be Pick drive\u2026")
        source_pick.setToolTip("Open live drive picker")
        source_pick.clicked.connect(self._pick_source)
        source_layout.addWidget(self.source_edit)
        source_layout.addWidget(source_browse)
        source_layout.addWidget(source_pick)
        root.addWidget(source_group)

        # Destination selection
        dest_group = QtWidgets.QGroupBox("Destination (must be empty)")
        dest_layout = QtWidgets.QHBoxLayout(dest_group)
        self.dest_edit = QtWidgets.QLineEdit()
        self.dest_edit.setPlaceholderText("/path/to/image.dd  — type path or use Browse")
        dest_browse = QtWidgets.QPushButton("Browse\u2026")
        dest_browse.setToolTip("Choose destination file")
        dest_browse.clicked.connect(self._browse_destination)
        dest_pick = QtWidgets.QPushButton("\U0001f4be Pick drive\u2026")
        dest_pick.setToolTip("Pick a destination drive (destructive!)")
        dest_pick.clicked.connect(self._pick_destination)
        dest_layout.addWidget(self.dest_edit)
        dest_layout.addWidget(dest_browse)
        dest_layout.addWidget(dest_pick)
        root.addWidget(dest_group)

        # Output log
        output_label = QtWidgets.QLabel("Output / Progress:")
        root.addWidget(output_label)

        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(220)
        self.output.setPlaceholderText("dd output will appear here once imaging starts\u2026")
        root.addWidget(self.output, stretch=1)

        self.status_label = QtWidgets.QLabel("Ready.")
        self.status_label.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        root.addWidget(self.status_label)

        btn_row = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("\u25b6  Start imaging")
        self.start_button.clicked.connect(self._start)
        self.clear_button = QtWidgets.QPushButton("Clear log")
        self.clear_button.clicked.connect(self.output.clear)
        btn_row.addStretch(1)
        btn_row.addWidget(self.clear_button)
        btn_row.addWidget(self.start_button)
        root.addLayout(btn_row)

    # --- Slots ---

    @QtCore.Slot()
    def _browse_source(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select source device or file", "/dev", "All files (*)",
        )
        if path:
            self.source_edit.setText(path)

    @QtCore.Slot()
    def _pick_source(self) -> None:
        dlg = DrivePickerDialog("Select source drive", self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted and dlg.selected_device:
            self.source_edit.setText(dlg.selected_device)

    @QtCore.Slot()
    def _browse_destination(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Select destination file", "",
            "Disk images (*.dd *.img *.raw);;All files (*)",
        )
        if path:
            self.dest_edit.setText(path)

    @QtCore.Slot()
    def _pick_destination(self) -> None:
        """Pick a destination drive — show an extra confirmation about data loss."""
        QtWidgets.QMessageBox.warning(
            self,
            "Destination warning",
            "You are about to select a DESTINATION DRIVE.\n\n"
            "ALL DATA on the selected drive will be permanently erased!\n"
            "Make sure the drive is empty or contains no data you need.",
        )
        dlg = DrivePickerDialog("Select destination drive (will be overwritten!)", self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted and dlg.selected_device:
            self.dest_edit.setText(dlg.selected_device)

    @QtCore.Slot()
    def _start(self) -> None:
        source = self.source_edit.text().strip()
        dest = self.dest_edit.text().strip()

        if not source or not dest:
            QtWidgets.QMessageBox.warning(
                self, "Missing information",
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


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


class Plugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(
            PluginMeta(
                name="Disk Imaging (dd)",
                description=(
                    "Create a bit-for-bit copy of a source drive to a "
                    "destination file or device using dd. "
                    "Use the live drive picker to browse connected devices."
                ),
                category="Acquisition",
            ),
        )

    def create_ui(self, parent: QtWidgets.QWidget) -> None:
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        widget = DDImagerWidget(parent)
        layout.addWidget(widget)
