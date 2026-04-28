"""Entry point for the Toolkit GUI application."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import load_config, AppConfig, ExternalToolConfig
from ..logging_utils import configure_logging
from ..plugins import discover_plugins, BasePlugin
from ..external_tool_installer import InstallWorker, is_tool_available


LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Landing-page card widgets
# ---------------------------------------------------------------------------


class PluginCard(QtWidgets.QFrame):
    """Card-style widget representing a single Python plugin."""

    def __init__(self, plugin: BasePlugin, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.plugin = plugin
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setObjectName("pluginCard")

        layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel(plugin.meta.name)
        title.setObjectName("pluginTitle")
        desc = QtWidgets.QLabel(plugin.meta.description)
        desc.setWordWrap(True)
        desc.setObjectName("pluginDescription")

        category = QtWidgets.QLabel(plugin.meta.category)
        category.setObjectName("pluginCategory")

        open_button = QtWidgets.QPushButton("Open")
        open_button.clicked.connect(self.open_plugin)

        layout.addWidget(title)
        layout.addWidget(desc)
        layout.addWidget(category)
        layout.addStretch(1)
        layout.addWidget(open_button, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

    @QtCore.Slot()
    def open_plugin(self) -> None:
        window = self.window()
        if isinstance(window, MainWindow):
            window.open_plugin(self.plugin)


class ExternalToolCard(QtWidgets.QFrame):
    """Card-style widget for a configured external tool.

    Automatically detects whether the tool is installed and shows a
    prominent \u2018Install\u2019 button when it is not.
    """

    def __init__(
        self,
        tool: ExternalToolConfig,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.tool = tool
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setObjectName("externalToolCard")

        layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel(tool.name)
        title.setObjectName("externalToolTitle")
        desc = QtWidgets.QLabel(tool.description)
        desc.setWordWrap(True)
        desc.setObjectName("externalToolDescription")

        self._status_label = QtWidgets.QLabel()
        self._status_label.setObjectName("toolStatusLabel")

        button_row = QtWidgets.QHBoxLayout()

        self._launch_button = QtWidgets.QPushButton("Launch")
        self._launch_button.clicked.connect(self.launch_tool)

        self._install_button = QtWidgets.QPushButton("\u2b07  Install")
        self._install_button.clicked.connect(self.install_tool)
        self._install_button.setObjectName("installButton")

        self._update_button = QtWidgets.QPushButton("Update\u2026")
        self._update_button.clicked.connect(self.update_tool)

        button_row.addWidget(self._install_button)
        button_row.addWidget(self._launch_button)
        button_row.addStretch(1)
        button_row.addWidget(self._update_button)

        layout.addWidget(title)
        layout.addWidget(desc)
        layout.addWidget(self._status_label)
        layout.addStretch(1)
        layout.addLayout(button_row)

        self._refresh_status()

    def _refresh_status(self) -> None:
        """Update the status label and button visibility based on tool availability."""
        available = is_tool_available(self.tool.command or [])
        if available:
            self._status_label.setText("\u2705  Installed")
            self._status_label.setStyleSheet("color: #6daa45;")
            self._launch_button.setEnabled(True)
            self._install_button.setVisible(False)
        else:
            self._status_label.setText("\u26a0\ufe0f  Not installed \u2014 click Install to set it up automatically")
            self._status_label.setStyleSheet("color: #bb653b;")
            self._launch_button.setEnabled(False)
            self._install_button.setVisible(True)

    @QtCore.Slot()
    def launch_tool(self) -> None:
        window = self.window()
        if isinstance(window, MainWindow):
            window.launch_external_tool(self.tool)

    @QtCore.Slot()
    def install_tool(self) -> None:
        window = self.window()
        if isinstance(window, MainWindow):
            window.install_external_tool(self.tool, on_done=self._refresh_status)

    @QtCore.Slot()
    def update_tool(self) -> None:
        window = self.window()
        if isinstance(window, MainWindow):
            window.update_external_tool(self.tool)


# ---------------------------------------------------------------------------
# Install progress dialog
# ---------------------------------------------------------------------------


class InstallDialog(QtWidgets.QDialog):
    """Blocking progress dialog shown while a tool is being installed."""

    def __init__(self, tool_name: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Installing {tool_name}\u2026")
        self.setMinimumWidth(600)
        self.setMinimumHeight(400)
        self.setModal(True)

        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QLabel(f"Installing <b>{tool_name}</b> and its dependencies\u2026")
        header.setWordWrap(True)
        layout.addWidget(header)

        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(260)
        layout.addWidget(self.output, stretch=1)

        self._spinner = QtWidgets.QProgressBar()
        self._spinner.setRange(0, 0)  # indeterminate
        layout.addWidget(self._spinner)

        self._close_button = QtWidgets.QPushButton("Close")
        self._close_button.setEnabled(False)
        self._close_button.clicked.connect(self.accept)
        layout.addWidget(self._close_button, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

    @QtCore.Slot(str)
    def append_log(self, line: str) -> None:
        self.output.appendPlainText(line)

    @QtCore.Slot(bool, str)
    def on_finished(self, success: bool, message: str) -> None:
        self._spinner.setRange(0, 1)
        self._spinner.setValue(1)
        self.output.appendPlainText(f"\n{'\u2705 Done' if success else '\u274c Failed'}: {message}")
        self._close_button.setEnabled(True)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QtWidgets.QMainWindow):
    """Main application window with landing page and plugin workspace."""

    def __init__(self) -> None:
        super().__init__()

        self.config: AppConfig = load_config()
        configure_logging(self.config)

        self.setWindowTitle(self.config.branding.window_title)
        self.resize(1200, 800)

        if self.config.branding.icon_path:
            icon_path = Path(self.config.branding.icon_path).expanduser()
            if icon_path.exists():
                self.setWindowIcon(QtGui.QIcon(str(icon_path)))

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        self.stacked = QtWidgets.QStackedWidget()
        self.landing_page = QtWidgets.QWidget()
        self.workspace = QtWidgets.QWidget()

        self._init_landing_page()
        self._init_workspace()

        self.stacked.addWidget(self.landing_page)
        self.stacked.addWidget(self.workspace)

        container_layout = QtWidgets.QVBoxLayout(central)
        container_layout.addWidget(self.stacked)

        self.status_bar = self.statusBar()
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)

        self._load_plugins()
        self._load_external_tools()

    def _init_landing_page(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.landing_page)

        header = QtWidgets.QLabel(self.config.branding.app_name)
        header.setObjectName("landingHeader")
        header.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        subtitle = QtWidgets.QLabel("Digital Forensics Toolkit")
        subtitle.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        subtitle.setObjectName("landingSubtitle")

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QtWidgets.QWidget()
        scroll.setWidget(scroll_content)

        scroll_layout = QtWidgets.QVBoxLayout(scroll_content)

        internal_label = QtWidgets.QLabel("Internal tools")
        internal_label.setObjectName("sectionHeader")
        self.plugin_grid = QtWidgets.QGridLayout()

        external_label = QtWidgets.QLabel("External tools")
        external_label.setObjectName("sectionHeader")
        self.external_grid = QtWidgets.QGridLayout()

        scroll_layout.addWidget(internal_label)
        scroll_layout.addLayout(self.plugin_grid)
        scroll_layout.addSpacing(24)
        scroll_layout.addWidget(external_label)
        scroll_layout.addLayout(self.external_grid)
        scroll_layout.addStretch(1)

        layout.addWidget(header)
        layout.addWidget(subtitle)
        layout.addWidget(scroll)

    def _init_workspace(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.workspace)

        self.workspace_header = QtWidgets.QLabel("")
        self.workspace_header.setObjectName("workspaceHeader")
        self.workspace_header.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)

        self.workspace_stack = QtWidgets.QStackedWidget()

        back_button = QtWidgets.QPushButton("\u2190  Back to tools")
        back_button.clicked.connect(self._go_home)

        layout.addWidget(self.workspace_header)
        layout.addWidget(self.workspace_stack, stretch=1)
        layout.addWidget(back_button, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

    def _load_plugins(self) -> None:
        LOGGER.info("Discovering plugins\u2026")
        plugins = discover_plugins()
        if not plugins:
            LOGGER.warning("No plugins discovered")

        row, col = 0, 0
        for plugin in plugins:
            card = PluginCard(plugin)
            self.plugin_grid.addWidget(card, row, col)
            col += 1
            if col >= 3:
                col, row = 0, row + 1

    def _load_external_tools(self) -> None:
        LOGGER.info("Loading external tools from config\u2026")
        if not self.config.external_tools:
            return

        row, col = 0, 0
        for tool in self.config.external_tools:
            card = ExternalToolCard(tool)
            self.external_grid.addWidget(card, row, col)
            col += 1
            if col >= 3:
                col, row = 0, row + 1

    @QtCore.Slot()
    def open_plugin(self, plugin: BasePlugin) -> None:
        LOGGER.info("Opening plugin %s", plugin.meta.name)
        self.status_bar.showMessage(f"Opening {plugin.meta.name}\u2026")
        self.progress_bar.setVisible(True)
        QtWidgets.QApplication.processEvents()

        # Each plugin gets a fresh container; the plugin is responsible for
        # building its own layout inside that container via create_ui().
        container = QtWidgets.QWidget()
        try:
            plugin.create_ui(container)
        except Exception:
            LOGGER.exception("Failed to initialize plugin %s", plugin.meta.name)
            error_layout = QtWidgets.QVBoxLayout(container)
            error_label = QtWidgets.QLabel(
                "\u274c  Failed to load plugin.\nSee the log file for details."
            )
            error_layout.addWidget(error_label)

        self.workspace_header.setText(plugin.meta.name)
        self.workspace_stack.addWidget(container)
        self.workspace_stack.setCurrentWidget(container)
        self.stacked.setCurrentWidget(self.workspace)

        self.status_bar.showMessage(f"{plugin.meta.name} loaded")
        self.progress_bar.setVisible(False)

    @QtCore.Slot()
    def _go_home(self) -> None:
        self.stacked.setCurrentWidget(self.landing_page)
        self.status_bar.showMessage("Ready")

    @QtCore.Slot()
    def launch_external_tool(self, tool: ExternalToolConfig) -> None:
        """Launch an external tool via its configured command."""
        import subprocess

        if not tool.command:
            QtWidgets.QMessageBox.warning(
                self, "Not configured",
                "No command configured for this external tool."
            )
            return

        if not is_tool_available(tool.command):
            QtWidgets.QMessageBox.warning(
                self, "Not installed",
                f"{tool.name} does not appear to be installed.\n"
                "Use the Install button to set it up automatically."
            )
            return

        try:
            subprocess.Popen(tool.command)
            self.status_bar.showMessage(f"Launched {tool.name}")
        except Exception:
            LOGGER.exception("Failed to launch external tool: %s", tool.command)
            QtWidgets.QMessageBox.critical(
                self, "Launch failed",
                "Failed to start the external tool. See the log for details.",
            )

    def install_external_tool(
        self,
        tool: ExternalToolConfig,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """Start an auto-installation for *tool*, showing a progress dialog."""
        from typing import Callable

        install_cfg = tool.install
        if not install_cfg:
            QtWidgets.QMessageBox.information(
                self, "No installer configured",
                "This tool does not have an install configuration in config.json."
            )
            return

        dialog = InstallDialog(tool.name, self)

        thread = QtCore.QThread(self)
        worker = InstallWorker(install_cfg)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log.connect(dialog.append_log)
        worker.finished.connect(dialog.on_finished)
        worker.finished.connect(thread.quit)
        if on_done:
            worker.finished.connect(lambda ok, _msg: on_done() if ok else None)

        thread.start()
        dialog.exec()

    @QtCore.Slot()
    def update_external_tool(self, tool: ExternalToolConfig) -> None:
        """Open an updater for the external tool."""
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        import subprocess

        if tool.update is None or tool.update.type is None:
            QtWidgets.QMessageBox.information(
                self, "No updater configured",
                "This tool does not have an update mechanism configured.",
            )
            return

        if tool.update.type == "url" and tool.update.url:
            QDesktopServices.openUrl(QUrl(tool.update.url))
            self.status_bar.showMessage(f"Opened update page for {tool.name}")
            return

        if tool.update.type == "command" and tool.update.command:
            try:
                subprocess.Popen(tool.update.command)
                self.status_bar.showMessage(f"Started updater for {tool.name}")
            except Exception:
                LOGGER.exception("Failed to start updater for %s", tool.name)
                QtWidgets.QMessageBox.critical(
                    self, "Update failed",
                    "Failed to start the update command. See the log for details.",
                )
            return

        QtWidgets.QMessageBox.warning(
            self, "Invalid update configuration",
            "The update configuration for this tool is invalid.",
        )


# ---------------------------------------------------------------------------
# Theme and entry point
# ---------------------------------------------------------------------------


def _apply_dark_theme(app: QtWidgets.QApplication) -> None:
    app.setStyle("Fusion")
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Window, QtGui.QColor(30, 30, 30))
    palette.setColor(QtGui.QPalette.WindowText, QtCore.Qt.GlobalColor.white)
    palette.setColor(QtGui.QPalette.Base, QtGui.QColor(45, 45, 45))
    palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(60, 60, 60))
    palette.setColor(QtGui.QPalette.ToolTipBase, QtCore.Qt.GlobalColor.white)
    palette.setColor(QtGui.QPalette.ToolTipText, QtCore.Qt.GlobalColor.white)
    palette.setColor(QtGui.QPalette.Text, QtCore.Qt.GlobalColor.white)
    palette.setColor(QtGui.QPalette.Button, QtGui.QColor(45, 45, 45))
    palette.setColor(QtGui.QPalette.ButtonText, QtCore.Qt.GlobalColor.white)
    palette.setColor(QtGui.QPalette.BrightText, QtCore.Qt.GlobalColor.red)
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(0, 120, 215))
    palette.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.GlobalColor.white)
    app.setPalette(palette)


def main() -> None:
    """Start the Qt application."""
    import sys

    app = QtWidgets.QApplication(sys.argv)
    _apply_dark_theme(app)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
