"""Entry point for the Toolkit GUI application."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import load_config, AppConfig, ExternalToolConfig
from ..logging_utils import configure_logging
from ..plugins import discover_plugins, BasePlugin


LOGGER = logging.getLogger(__name__)


class PluginCard(QtWidgets.QFrame):
  """Card-style widget representing a single plugin on the landing page."""

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

    open_button = QtWidgets.QPushButton("Open")
    open_button.clicked.connect(self.open_plugin)

    layout.addWidget(title)
    layout.addWidget(desc)
    layout.addStretch(1)
    layout.addWidget(open_button, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

  @QtCore.Slot()
  def open_plugin(self) -> None:
    window = self.window()
    if isinstance(window, MainWindow):
      window.open_plugin(self.plugin)


class ExternalToolCard(QtWidgets.QFrame):
  """Card-style widget representing a configured external tool."""

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

    button_row = QtWidgets.QHBoxLayout()
    launch_button = QtWidgets.QPushButton("Launch")
    launch_button.clicked.connect(self.launch_tool)
    button_row.addWidget(launch_button)

    update_button = QtWidgets.QPushButton("Update…")
    update_button.clicked.connect(self.update_tool)
    button_row.addWidget(update_button)

    layout.addWidget(title)
    layout.addWidget(desc)
    layout.addStretch(1)
    layout.addLayout(button_row)

  @QtCore.Slot()
  def launch_tool(self) -> None:
    window = self.window()
    if isinstance(window, MainWindow):
      window.launch_external_tool(self.tool)

  @QtCore.Slot()
  def update_tool(self) -> None:
    window = self.window()
    if isinstance(window, MainWindow):
      window.update_external_tool(self.tool)


class MainWindow(QtWidgets.QMainWindow):
  """Main application window hosting the plugin landing page and workspace."""

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
    self.progress_bar.setRange(0, 0)  # Busy indicator
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

    back_button = QtWidgets.QPushButton("Back to tools")
    back_button.clicked.connect(self._go_home)

    layout.addWidget(self.workspace_header)
    layout.addWidget(self.workspace_stack, stretch=1)
    layout.addWidget(back_button, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

  def _load_plugins(self) -> None:
    LOGGER.info("Discovering plugins…")
    plugins = discover_plugins()
    if not plugins:
      LOGGER.warning("No plugins discovered")

    row = 0
    col = 0
    for plugin in plugins:
      card = PluginCard(plugin)
      self.plugin_grid.addWidget(card, row, col)
      col += 1
      if col >= 3:
        col = 0
        row += 1

  def _load_external_tools(self) -> None:
    LOGGER.info("Loading external tools from config…")
    if not self.config.external_tools:
      return

    row = 0
    col = 0
    for tool in self.config.external_tools:
      card = ExternalToolCard(tool)
      self.external_grid.addWidget(card, row, col)
      col += 1
      if col >= 3:
        col = 0
        row += 1

  @QtCore.Slot(BasePlugin)
  def open_plugin(self, plugin: BasePlugin) -> None:
    LOGGER.info("Opening plugin %s", plugin.meta.name)
    self.status_bar.showMessage(f"Opening {plugin.meta.name}…")
    self.progress_bar.setVisible(True)
    QtWidgets.QApplication.processEvents()

    container = QtWidgets.QWidget()
    try:
      plugin.create_ui(container)
    except Exception:  # pragma: no cover - defensive
      LOGGER.exception("Failed to initialize plugin %s", plugin.meta.name)
      layout = QtWidgets.QVBoxLayout(container)
      error_label = QtWidgets.QLabel("Failed to load plugin. See log for details.")
      layout.addWidget(error_label)

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

  @QtCore.Slot(ExternalToolConfig)
  def launch_external_tool(self, tool: ExternalToolConfig) -> None:
    """Launch an external tool as configured in config.json."""

    import subprocess

    if not tool.command:
      QtWidgets.QMessageBox.warning(self, "Not configured", "No command configured for this external tool.")
      return

    try:
      subprocess.Popen(tool.command)
      self.status_bar.showMessage(f"Launched {tool.name}")
    except FileNotFoundError:
      LOGGER.exception("External tool not found: %s", tool.command)
      QtWidgets.QMessageBox.critical(
        self,
        "Tool not found",
        "The configured external tool could not be found. Check the path in config.json.",
      )
    except Exception:  # pragma: no cover - defensive
      LOGGER.exception("Failed to launch external tool: %s", tool.command)
      QtWidgets.QMessageBox.critical(
        self,
        "Launch failed",
        "Failed to start the external tool. See the log for details.",
      )

  @QtCore.Slot(ExternalToolConfig)
  def update_external_tool(self, tool: ExternalToolConfig) -> None:
    """Open an updater for the external tool (URL or command)."""

    from PySide6.QtGui import QDesktopServices
    from PySide6.QtCore import QUrl
    import subprocess

    if tool.update is None or tool.update.type is None:
      QtWidgets.QMessageBox.information(
        self,
        "No updater configured",
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
      except Exception:  # pragma: no cover - defensive
        LOGGER.exception("Failed to start updater for %s", tool.name)
        QtWidgets.QMessageBox.critical(
          self,
          "Update failed",
          "Failed to start the update command. See the log for details.",
        )
      return

    QtWidgets.QMessageBox.warning(
      self,
      "Invalid update configuration",
      "The update configuration for this tool is invalid.",
    )


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


if __name__ == "__main__":  # pragma: no cover - manual launch
  main()
