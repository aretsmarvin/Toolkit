"""Disk imaging plugin using `dd`.

The dd process runs in a QThread so the GUI never freezes.

Privilege handling
------------------
`dd` requires read access to raw block devices (/dev/diskN), which needs
elevated privileges on macOS. Rather than running the entire application
as root (which is insecure), only the `dd` command itself is executed with
administrator privileges via osascript. This causes macOS to display a
native authentication dialog to the user before any data is read or written.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from . import BasePlugin, PluginMeta


LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Drive enumeration (macOS diskutil)
# ---------------------------------------------------------------------------


def list_drives() -> list[dict]:
    """Return a list of connected drives/partitions via diskutil.

    Each entry has keys: device, name, size, removable.
    Returns an empty list on any error.
    """
    drives: list[dict] = []
    try:
        result = subprocess.run(
            ["diskutil", "list", "-plist"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        import plistlib
        data = plistlib.loads(result.stdout.encode())
        for disk in data.get("AllDisksAndPartitions", []):
            dev = disk.get("DeviceIdentifier", "")
            drives.append({
                "device": f"/dev/{dev}",
                "name": disk.get("VolumeName", "") or f"Disk ({dev})",
                "size": disk.get("Size", 0),
                "removable": disk.get("RemovableMedia", False),
            })
            for part in disk.get("Partitions", []):
                pdev = part.get("DeviceIdentifier", "")
                drives.append({
                    "device": f"/dev/{pdev}",
                    "name": part.get("VolumeName", "") or pdev,
                    "size": part.get("Size", 0),
                    "removable": disk.get("RemovableMedia", False),
                })
    except Exception as exc:
        LOGGER.warning("Could not enumerate drives: %s", exc)
    return drives


def format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# ---------------------------------------------------------------------------
# Drive picker dialog
# ---------------------------------------------------------------------------


class DrivePickerDialog(QtWidgets.QDialog):
    """Modal dialog listing all connected drives for selection."""

    def __init__(self, title: str = "Select drive", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(620)
        self.selected_device: str | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("Select a drive or partition:"))

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Device", "Name", "Size", "Removable"])
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
        for drive in list_drives():
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(drive["device"]))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(drive["name"]))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(format_size(drive["size"])))
            self.table.setItem(
                row, 3,
                QtWidgets.QTableWidgetItem("Yes" if drive["removable"] else "No")
            )

    @QtCore.Slot()
    def _accept(self) -> None:
        if self.table.currentRow() >= 0:
            self.selected_device = self.table.item(self.table.currentRow(), 0).text()
            self.accept()
        else:
            QtWidgets.QMessageBox.warning(self, "No selection", "Please select a drive.")


# ---------------------------------------------------------------------------
# dd worker  —  runs dd with sudo via osascript (native password dialog)
# ---------------------------------------------------------------------------


class DDWorker(QtCore.QObject):
    """Execute dd with administrator privileges in a background thread.

    Privilege model
    ---------------
    Only the dd command runs as root via osascript. The rest of the
    application stays unprivileged. The user sees a standard macOS
    \u2018Authentication Required\u2019 dialog before any data transfer begins.

    Progress
    --------
    macOS dd (and GNU coreutils dd) write status to stderr when
    status=progress is set.  We redirect stderr to stdout in the osascript
    shell invocation and read stdout from the Popen subprocess.
    """

    started = QtCore.Signal()
    finished = QtCore.Signal(int)   # exit code (0 = success)
    progress = QtCore.Signal(str)   # a line of dd output
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

        src = shlex.quote(self._source)
        dst = shlex.quote(self._destination)

        # Build the dd shell command.  We use /dev/rdiskN (raw device) when
        # the source looks like a disk node — raw devices are significantly
        # faster on macOS because they bypass the buffer cache.
        src_path = self._source
        if src_path.startswith("/dev/disk"):
            src_path = src_path.replace("/dev/disk", "/dev/rdisk", 1)
            src = shlex.quote(src_path)
            LOGGER.info("Using raw device: %s", src_path)

        shell_cmd = (
            f"dd if={src} of={dst} bs=1m status=progress 2>&1"
        )

        # Wrap in osascript so the password prompt is a macOS dialog,
        # never a terminal prompt.
        apple_script = (
            f'do shell script "{shell_cmd}" '
            'with administrator privileges '
            'without altering line endings'
        )

        LOGGER.info("Starting dd (privileged): %s", shell_cmd)

        try:
            process = subprocess.Popen(
                ["osascript", "-e", apple_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            msg = "osascript not found — is this macOS?"
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
    """Full dd imaging UI with drive picker and privileged execution."""

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

        # Privilege info banner
        priv_info = QtWidgets.QLabel(
            "\U0001f512 <b>Administrator access required.</b> "
            "When you click <i>Start imaging</i>, macOS will ask for your "
            "password via a secure dialog. Only <code>dd</code> runs as root "
            "— the rest of this application stays unprivileged."
        )
        priv_info.setWordWrap(True)
        priv_info.setStyleSheet(
            "background: #1a3a5a; color: #cce4ff;"
            "border-radius: 6px; padding: 10px;"
        )
        root.addWidget(priv_info)

        # Source
        source_group = QtWidgets.QGroupBox("Source device")
        source_layout = QtWidgets.QHBoxLayout(source_group)
        self.source_edit = QtWidgets.QLineEdit()
        self.source_edit.setPlaceholderText("/dev/disk2  — type path or use Browse / Pick drive")
        source_browse = QtWidgets.QPushButton("Browse\u2026")
        source_browse.clicked.connect(self._browse_source)
        source_pick = QtWidgets.QPushButton("\U0001f4be Pick drive\u2026")
        source_pick.clicked.connect(self._pick_source)
        source_layout.addWidget(self.source_edit)
        source_layout.addWidget(source_browse)
        source_layout.addWidget(source_pick)
        root.addWidget(source_group)

        # Destination
        dest_group = QtWidgets.QGroupBox("Destination (must be empty)")
        dest_layout = QtWidgets.QHBoxLayout(dest_group)
        self.dest_edit = QtWidgets.QLineEdit()
        self.dest_edit.setPlaceholderText("/path/to/image.dd  — type path or use Browse")
        dest_browse = QtWidgets.QPushButton("Browse\u2026")
        dest_browse.clicked.connect(self._browse_destination)
        dest_pick = QtWidgets.QPushButton("\U0001f4be Pick drive\u2026")
        dest_pick.clicked.connect(self._pick_destination)
        dest_layout.addWidget(self.dest_edit)
        dest_layout.addWidget(dest_browse)
        dest_layout.addWidget(dest_pick)
        root.addWidget(dest_group)

        # Output
        root.addWidget(QtWidgets.QLabel("Output / Progress:"))
        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(220)
        self.output.setPlaceholderText(
            "dd output will appear here once imaging starts\u2026"
        )
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

    # --- Browse / Pick slots ---

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

    # --- Start imaging ---

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
                "macOS will ask for your administrator password.\n\n"
                "Continue?"
            ),
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.start_button.setEnabled(False)
        self.output.clear()
        self.status_label.setText("Waiting for macOS password dialog\u2026")
        self.output.appendPlainText(
            "macOS will now show a password dialog to authorise dd.\n"
            "Please enter your administrator password there."
        )
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
            QtWidgets.QMessageBox.information(
                self, "Imaging complete", "dd finished successfully."
            )
        else:
            self.status_label.setText(f"dd exited with code {rc}.")
            LOGGER.warning("dd imaging failed with exit code %d", rc)
            QtWidgets.QMessageBox.warning(
                self, "Imaging failed",
                f"dd exited with code {rc}.\n"
                "If you cancelled the password dialog, please try again.",
            )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


class Plugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(
            PluginMeta(
                name="Disk Imaging (dd)",
                description=(
                    "Create a bit-for-bit copy of a source drive using dd. "
                    "Administrator access is requested only for the dd command "
                    "itself via a secure macOS dialog."
                ),
                category="Acquisition",
            ),
        )

    def create_ui(self, parent: QtWidgets.QWidget) -> None:
        layout = QtWidgets.QVBoxLayout(parent)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(DDImagerWidget(parent))
