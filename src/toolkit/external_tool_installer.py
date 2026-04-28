"""Auto-installer for external forensic tools.

This module is responsible for detecting whether a configured external tool
is installed and, if not, downloading and installing it automatically
(including its dependencies). Installation logic is pluggable: each tool
entry in config.json can specify an ``install`` block that drives this
module.

Currently supported install strategies on macOS:
  - ``homebrew_cask``  : install via ``brew install --cask <cask>``
  - ``homebrew_formula``: install via ``brew install <formula>``
  - ``dmg``            : download a .dmg, mount it, copy the .app bundle,
                         unmount (basic heuristic; works for most .dmg
                         distributions).
  - ``pkg``            : download and run a .pkg installer via
                         ``installer -pkg ... -target /``.

Homebrew itself is auto-installed if missing (non-interactively via the
official install script).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from PySide6 import QtCore


LOGGER = logging.getLogger(__name__)

_HOMEBREW_INSTALL_URL = (
    "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
)


class InstallWorker(QtCore.QObject):
    """Run an installation in a background thread.

    Signals
    -------
    log(str)
        A human-readable progress line suitable for display in a QPlainTextEdit.
    finished(bool, str)
        Emitted when the installation attempt ends.
        *bool* is True on success; *str* is a message.
    """

    log = QtCore.Signal(str)
    finished = QtCore.Signal(bool, str)

    def __init__(
        self,
        install_config: dict,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = install_config

    @QtCore.Slot()
    def run(self) -> None:
        strategy = self._cfg.get("strategy")
        try:
            if strategy == "homebrew_cask":
                self._homebrew_install(self._cfg["cask"], cask=True)
            elif strategy == "homebrew_formula":
                self._homebrew_install(self._cfg["formula"], cask=False)
            elif strategy == "dmg":
                self._dmg_install(
                    self._cfg["url"],
                    self._cfg.get("app_name"),
                )
            elif strategy == "pkg":
                self._pkg_install(self._cfg["url"])
            else:
                self.finished.emit(False, f"Unknown install strategy: {strategy!r}")
                return
        except Exception as exc:
            LOGGER.exception("Installation failed")
            self.finished.emit(False, str(exc))
            return

        self.finished.emit(True, "Installation completed successfully.")

    # ------------------------------------------------------------------
    # Strategy implementations
    # ------------------------------------------------------------------

    def _run(self, cmd: list[str], env: dict | None = None) -> None:
        """Run *cmd*, emitting each output line as a log signal."""
        LOGGER.debug("Running: %s", cmd)
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        # Make Homebrew happy in non-interactive environments.
        full_env.setdefault("NONINTERACTIVE", "1")
        full_env.setdefault("CI", "1")

        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=full_env,
        ) as proc:
            assert proc.stdout
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    LOGGER.debug(line)
                    self.log.emit(line)
            rc = proc.wait()

        if rc != 0:
            raise RuntimeError(f"Command exited with code {rc}: {cmd}")

    def _ensure_homebrew(self) -> str:
        """Return the path to `brew`, installing Homebrew first if absent."""
        brew = shutil.which("brew") or "/opt/homebrew/bin/brew" if Path("/opt/homebrew/bin/brew").exists() else "/usr/local/bin/brew" if Path("/usr/local/bin/brew").exists() else None
        if brew and Path(brew).exists():
            return brew

        self.log.emit("Homebrew not found — downloading and installing Homebrew\u2026")
        LOGGER.info("Installing Homebrew")
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".sh", delete=False
        ) as tmp:
            with urllib.request.urlopen(_HOMEBREW_INSTALL_URL) as resp:
                tmp.write(resp.read())
            script = tmp.name

        os.chmod(script, 0o755)
        self._run(["/bin/bash", script])
        os.unlink(script)

        for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
            if Path(candidate).exists():
                return candidate

        raise RuntimeError("Homebrew installation finished but `brew` not found.")

    def _homebrew_install(self, name: str, *, cask: bool) -> None:
        brew = self._ensure_homebrew()
        self.log.emit(f"Updating Homebrew\u2026")
        self._run([brew, "update"])
        flag = "--cask" if cask else ""
        kind = "cask" if cask else "formula"
        self.log.emit(f"Installing {kind} \u2018{name}\u2019 via Homebrew\u2026")
        cmd = [brew, "install"] + (["--cask"] if cask else []) + [name]
        self._run(cmd)

    def _dmg_install(self, url: str, app_name: str | None) -> None:
        self.log.emit(f"Downloading {url}\u2026")
        LOGGER.info("Downloading DMG from %s", url)
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".dmg", delete=False
        ) as tmp:
            with urllib.request.urlopen(url) as resp:
                chunk = resp.read(65536)
                while chunk:
                    tmp.write(chunk)
                    chunk = resp.read(65536)
            dmg_path = tmp.name

        self.log.emit("Mounting DMG\u2026")
        result = subprocess.run(
            ["hdiutil", "attach", dmg_path, "-nobrowse", "-quiet"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"hdiutil attach failed: {result.stderr}")

        # Parse mount point from hdiutil output (last tab-separated column)
        mount_point = None
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[-1].strip().startswith("/Volumes"):
                mount_point = parts[-1].strip()
                break

        if not mount_point:
            raise RuntimeError("Could not determine DMG mount point.")

        try:
            apps = list(Path(mount_point).glob("*.app"))
            if not apps:
                raise RuntimeError("No .app bundle found in DMG.")
            app_bundle = apps[0]
            dest = Path("/Applications") / app_bundle.name
            self.log.emit(f"Copying {app_bundle.name} to /Applications\u2026")
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(str(app_bundle), str(dest))
            self.log.emit(f"Installed to {dest}")
        finally:
            subprocess.run(
                ["hdiutil", "detach", mount_point, "-quiet"],
                capture_output=True,
            )
            os.unlink(dmg_path)

    def _pkg_install(self, url: str) -> None:
        self.log.emit(f"Downloading {url}\u2026")
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".pkg", delete=False
        ) as tmp:
            with urllib.request.urlopen(url) as resp:
                chunk = resp.read(65536)
                while chunk:
                    tmp.write(chunk)
                    chunk = resp.read(65536)
            pkg_path = tmp.name

        self.log.emit("Running installer (may require admin password)\u2026")
        self._run(["sudo", "-n", "installer", "-pkg", pkg_path, "-target", "/"])
        os.unlink(pkg_path)


def is_tool_available(command: list[str]) -> bool:
    """Return True if the first element of *command* resolves to an executable,
    OR if `open -a <app>` would succeed.
    """
    if not command:
        return False

    first = command[0]

    # `open -a AppName` strategy
    if first == "open" and len(command) >= 3 and command[1] == "-a":
        app_name = command[2]
        result = subprocess.run(
            ["open", "-Ra", app_name],
            capture_output=True,
        )
        return result.returncode == 0

    # direct executable strategy
    return shutil.which(first) is not None or Path(first).exists()
