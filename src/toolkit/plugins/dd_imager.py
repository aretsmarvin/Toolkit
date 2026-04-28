"""Demo plugin that wraps `dd` for disk imaging.

This is intentionally conservative and focused on clarity; in a real
forensics environment you would likely integrate `dcfldd` or `ddrescue`
and add verification and hashing.
"""
from __future__ import annotations

import logging
import shlex
import subprocess

from PySide6 import QtCore, QtWidgets

from . import BasePlugin, PluginMeta


LOGGER = logging.getLogger(__name__)


class DDWorker(QtCore.QObject):
  """Run `dd` in a worker thread to keep the GUI responsive."""

  started = QtCore.Signal()
  finished = QtCore.Signal(int)
  progress = QtCore.Signal(str)
  error = QtCore.Signal(str)

  def __init__(self, source: str, destination: str, parent: QtCore.QObject | None = None) -> None:
    super().__init__(parent)
    self._source = source
    self._destination = destination

  @QtCore.Slot()
  def run(self) -> None:
    self.started.emit()
    cmd = ["dd", f"if={self._source}", f"of={self._destination}", "bs=1m", "status=progress"]
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
    except Exception as exc:  # pragma: no cover - defensive
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


class Plugin(BasePlugin):
  def __init__(self) -> None:
    super().__init__(
      PluginMeta(
        name="Disk imaging (dd demo)",
        description="Create a bit-for-bit copy of a source device to a destination file or device using dd.",
        category="Acquisition",
      ),
    )

  def create_ui(self, parent) -> None:  # pragma: no cover - Qt runtime
    layout = QtWidgets.QVBoxLayout(parent)

    warning = QtWidgets.QLabel(
      "<b>Warning:</b> Destination must be an empty disk or file. All data on the destination will be overwritten.",
    )
    warning.setWordWrap(True)

    form = QtWidgets.QFormLayout()

    self.source_edit = QtWidgets.QLineEdit("/dev/diskX")
    self.dest_edit = QtWidgets.QLineEdit("/path/to/image.dd")

    form.addRow("Source device:", self.source_edit)
    form.addRow("Destination path/device:", self.dest_edit)

    self.output = QtWidgets.QPlainTextEdit()
    self.output.setReadOnly(True)
    self.output.setMinimumHeight(200)

    self.start_button = QtWidgets.QPushButton("Start imaging")
    self.start_button.clicked.connect(self._start)

    layout.addWidget(warning)
    layout.addLayout(form)
    layout.addWidget(self.start_button)
    layout.addWidget(self.output)

  @QtCore.Slot()
  def _start(self) -> None:
    source = self.source_edit.text().strip()
    dest = self.dest_edit.text().strip()

    if not source or not dest:
      QtWidgets.QMessageBox.warning(
        None,
        "Missing information",
        "Please provide both a source device and a destination path.",
      )
      return

    confirm = QtWidgets.QMessageBox.question(
      None,
      "Confirm destructive operation",
      "Imaging will overwrite the destination completely. Ensure the destination is empty. Continue?",
    )
    if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
      return

    self.start_button.setEnabled(False)
    self.output.clear()

    self._thread = QtCore.QThread(self)
    self._worker = DDWorker(source, dest)
    self._worker.moveToThread(self._thread)

    self._thread.started.connect(self._worker.run)
    self._worker.started.connect(lambda: LOGGER.info("dd imaging started"))
    self._worker.progress.connect(self._on_progress)
    self._worker.error.connect(self._on_error)
    self._worker.finished.connect(self._on_finished)
    self._worker.finished.connect(self._thread.quit)

    self._thread.start()

  @QtCore.Slot(str)
  def _on_progress(self, line: str) -> None:
    self.output.appendPlainText(line)

  @QtCore.Slot(str)
  def _on_error(self, message: str) -> None:
    QtWidgets.QMessageBox.critical(None, "dd error", message)

  @QtCore.Slot(int)
  def _on_finished(self, rc: int) -> None:
    self.start_button.setEnabled(True)
    if rc == 0:
      QtWidgets.QMessageBox.information(None, "Imaging complete", "dd finished successfully.")
    else:
      QtWidgets.QMessageBox.warning(None, "Imaging failed", f"dd exited with code {rc}.")
