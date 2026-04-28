"""Auto-installer for external forensic tools.

Supported install strategies on macOS
--------------------------------------
homebrew_cask      : brew install --cask <cask>
homebrew_formula   : brew install <formula>
autopsy_macos      : Full Autopsy macOS install:
                     1. Install Java (liberica-jdk8-full) via Homebrew
                     2. Install sleuthkit via Homebrew
                     3. Install testdisk (photorec) via Homebrew
                     4. Download Autopsy ZIP from GitHub
                     5. Extract to ~/Applications/
                     6. Run unix_setup.sh
                     7. Create a launcher shell script + macOS .app wrapper
dmg                : Download .dmg, mount, copy .app to /Applications
pkg                : Download .pkg, run installer
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from PySide6 import QtCore


LOGGER = logging.getLogger(__name__)

_HOMEBREW_INSTALL_URL = (
    "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
)

# Latest Autopsy release ZIP (macOS/Linux universal).
# Update this constant when a new Autopsy version is released.
_AUTOPSY_VERSION = "4.22.1"
_AUTOPSY_ZIP_URL = (
    f"https://github.com/sleuthkit/autopsy/releases/download/"
    f"autopsy-{_AUTOPSY_VERSION}/autopsy-{_AUTOPSY_VERSION}_v2.zip"
)
_AUTOPSY_INSTALL_DIR = Path.home() / "Applications" / f"autopsy-{_AUTOPSY_VERSION}"
_AUTOPSY_LAUNCHER = Path.home() / "Applications" / "Autopsy.app"


class InstallWorker(QtCore.QObject):
    """Run an installation in a background QThread.

    Signals
    -------
    log(str)
        Human-readable progress line for display in a QPlainTextEdit.
    finished(bool, str)
        True on success; str is a result message.
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
            elif strategy == "autopsy_macos":
                self._autopsy_macos_install()
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
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(self, cmd: list[str], env: dict | None = None) -> None:
        """Run *cmd*, emitting each output line as a log signal."""
        LOGGER.debug("Running: %s", cmd)
        full_env = os.environ.copy()
        # Keep PATH useful for subprocesses.
        for extra in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
            if extra not in full_env.get("PATH", ""):
                full_env["PATH"] = extra + ":" + full_env.get("PATH", "")
        if env:
            full_env.update(env)
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
        """Return path to `brew`, installing Homebrew non-interactively if absent."""
        for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
            if Path(candidate).exists():
                return candidate

        found = shutil.which("brew")
        if found:
            return found

        self.log.emit("Homebrew not found — downloading and installing Homebrew…")
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".sh", delete=False) as tmp:
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
        self.log.emit("Updating Homebrew…")
        self._run([brew, "update"])
        kind = "cask" if cask else "formula"
        self.log.emit(f"Installing {kind} '{name}' via Homebrew…")
        cmd = [brew, "install"] + (["--cask"] if cask else []) + [name]
        self._run(cmd)

    # ------------------------------------------------------------------
    # Autopsy macOS full installer
    # ------------------------------------------------------------------

    def _autopsy_macos_install(self) -> None:
        """Install Autopsy and all its macOS dependencies from scratch.

        Steps
        -----
        1. Install Homebrew if missing
        2. Install bellsoft-liberica-jdk8-full (Java 8 — required by Autopsy)
        3. Install sleuthkit (The Sleuth Kit)
        4. Install testdisk (ships PhotoRec)
        5. Install gstreamer (media codec support)
        6. Download Autopsy ZIP from GitHub releases
        7. Extract to ~/Applications/autopsy-<version>/
        8. Run unix_setup.sh inside the extracted folder
        9. Create ~/Applications/Autopsy.app launcher so `open -a Autopsy` works
        """
        brew = self._ensure_homebrew()
        self.log.emit("Updating Homebrew…")
        self._run([brew, "update"])

        # --- Java 8 (Liberica full JDK, required by Autopsy NetBeans platform) ---
        self.log.emit("Step 1/5: Installing Java 8 (Liberica JDK)…")
        # Tap the BellSoft repo first
        self._run([brew, "tap", "bell-sw/liberica"])
        self._run([brew, "install", "--cask", "bell-sw/liberica/liberica-jdk8-full"])

        # Locate JAVA_HOME
        java_home = self._find_java_home()
        self.log.emit(f"Java home: {java_home}")

        # --- The Sleuth Kit ---
        self.log.emit("Step 2/5: Installing The Sleuth Kit…")
        self._run([brew, "install", "sleuthkit"])

        # --- TestDisk / PhotoRec ---
        self.log.emit("Step 3/5: Installing TestDisk / PhotoRec…")
        self._run([brew, "install", "testdisk"])

        # --- GStreamer ---
        self.log.emit("Step 4/5: Installing GStreamer…")
        self._run([brew, "install", "gstreamer"])

        # --- Download Autopsy ZIP ---
        self.log.emit(f"Step 5/5: Downloading Autopsy {_AUTOPSY_VERSION}…")
        zip_url = self._cfg.get("zip_url", _AUTOPSY_ZIP_URL)
        zip_path = self._download_file(zip_url, suffix=".zip")

        # Extract
        install_parent = Path.home() / "Applications"
        install_parent.mkdir(parents=True, exist_ok=True)

        self.log.emit(f"Extracting Autopsy to {install_parent}…")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(install_parent)
        os.unlink(zip_path)

        # Discover extracted directory (it may vary in name)
        autopsy_dir = self._find_autopsy_dir(install_parent)
        self.log.emit(f"Autopsy extracted to: {autopsy_dir}")

        # Make scripts executable
        for script_name in ("unix_setup.sh", "bin/autopsy"):
            script = autopsy_dir / script_name
            if script.exists():
                script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

        # Locate sleuthkit jar (Homebrew puts it under share/java)
        tsk_jar = self._find_sleuthkit_jar()
        self.log.emit(f"Sleuth Kit JAR: {tsk_jar}")

        # Run unix_setup.sh
        self.log.emit("Running unix_setup.sh…")
        setup_env = {
            "JAVA_HOME": java_home,
            "TSK_JAR_PATH": tsk_jar,
        }
        setup_script = autopsy_dir / "unix_setup.sh"
        self._run([str(setup_script)], env=setup_env)

        # --- Create macOS .app launcher ---
        self.log.emit("Creating Autopsy.app launcher…")
        self._create_autopsy_app(autopsy_dir, java_home)

        self.log.emit("\n✅ Autopsy installation complete.")
        self.log.emit(f"   Launch via: open -a Autopsy")
        self.log.emit(f"   Or run:     {autopsy_dir}/bin/autopsy")

    def _find_java_home(self) -> str:
        """Return JAVA_HOME for the installed Liberica JDK."""
        # Try /usr/libexec/java_home first
        result = subprocess.run(
            ["/usr/libexec/java_home", "-v", "1.8"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

        # Fallback: scan known locations
        for base in (
            Path("/Library/Java/JavaVirtualMachines"),
            Path.home() / "Library" / "Java" / "JavaVirtualMachines",
        ):
            if not base.exists():
                continue
            for jdk in sorted(base.iterdir(), reverse=True):
                home = jdk / "Contents" / "Home"
                if home.exists():
                    return str(home)

        raise RuntimeError(
            "Could not locate Java 8 home. "
            "Please check that Liberica JDK 8 was installed correctly."
        )

    def _find_sleuthkit_jar(self) -> str:
        """Return path to the sleuthkit-*.jar installed by Homebrew."""
        for base in (
            Path("/opt/homebrew/share/java"),
            Path("/usr/local/share/java"),
        ):
            if not base.exists():
                continue
            jars = sorted(base.glob("sleuthkit-*.jar"), reverse=True)
            if jars:
                return str(jars[0])
        raise RuntimeError(
            "Could not locate the Sleuth Kit JAR. "
            "Make sure `brew install sleuthkit` succeeded."
        )

    @staticmethod
    def _find_autopsy_dir(parent: Path) -> Path:
        """Return the autopsy-X.X.X directory inside *parent*."""
        candidates = sorted(parent.glob("autopsy-*"), reverse=True)
        for c in candidates:
            if c.is_dir() and (c / "bin" / "autopsy").exists():
                return c
        # Fallback — take anything named autopsy-*
        for c in candidates:
            if c.is_dir():
                return c
        raise RuntimeError(f"Could not find extracted Autopsy directory inside {parent}.")

    def _create_autopsy_app(
        self, autopsy_dir: Path, java_home: str
    ) -> None:
        """Create a minimal macOS .app bundle that launches Autopsy."""
        app_path = Path.home() / "Applications" / "Autopsy.app"
        macos_dir = app_path / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True, exist_ok=True)

        # Info.plist
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
            ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            '  <key>CFBundleName</key><string>Autopsy</string>\n'
            '  <key>CFBundleDisplayName</key><string>Autopsy</string>\n'
            '  <key>CFBundleIdentifier</key>\n'
            '  <string>org.sleuthkit.autopsy</string>\n'
            '  <key>CFBundleVersion</key>\n'
            f'  <string>{_AUTOPSY_VERSION}</string>\n'
            '  <key>CFBundleExecutable</key><string>autopsy-launcher</string>\n'
            '  <key>LSMinimumSystemVersion</key><string>10.13</string>\n'
            '</dict></plist>\n'
        )
        (app_path / "Contents" / "Info.plist").write_text(plist)

        # Launcher shell script
        launcher_script = (
            "#!/bin/bash\n"
            f'export JAVA_HOME="{java_home}"\n'
            f'export PATH="$JAVA_HOME/bin:$PATH"\n'
            f'exec "{autopsy_dir}/bin/autopsy" "$@"\n'
        )
        launcher = macos_dir / "autopsy-launcher"
        launcher.write_text(launcher_script)
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        LOGGER.info("Autopsy.app created at %s", app_path)

    def _download_file(self, url: str, suffix: str = "") -> str:
        """Download *url* to a temp file, streaming progress. Returns path."""
        self.log.emit(f"Downloading {url}…")
        req = urllib.request.Request(url, headers={"User-Agent": "Toolkit-Installer/1.0"})
        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=suffix, delete=False
            ) as tmp:
                path = tmp.name
                chunk = resp.read(131072)
                while chunk:
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded / total * 100)
                        self.log.emit(f"  {pct}%  ({downloaded:,} / {total:,} bytes)")
                    chunk = resp.read(131072)
        self.log.emit(f"Download complete: {path}")
        return path

    # ------------------------------------------------------------------
    # DMG / PKG strategies
    # ------------------------------------------------------------------

    def _dmg_install(self, url: str, app_name: str | None) -> None:
        dmg_path = self._download_file(url, suffix=".dmg")
        self.log.emit("Mounting DMG…")
        result = subprocess.run(
            ["hdiutil", "attach", dmg_path, "-nobrowse", "-quiet"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"hdiutil attach failed: {result.stderr}")

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
            self.log.emit(f"Copying {app_bundle.name} to /Applications…")
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
        pkg_path = self._download_file(url, suffix=".pkg")
        self.log.emit("Running installer (may require admin password)…")
        self._run(["sudo", "-n", "installer", "-pkg", pkg_path, "-target", "/"])
        os.unlink(pkg_path)


# ---------------------------------------------------------------------------
# Public utility
# ---------------------------------------------------------------------------


def is_tool_available(command: list[str]) -> bool:
    """Return True if the tool described by *command* appears to be installed.

    Handles both ``open -a AppName`` and direct executable commands.
    Also checks for the Autopsy.app launcher created by this installer.
    """
    if not command:
        return False

    first = command[0]

    # `open -a AppName` strategy — also handles our custom .app wrapper
    if first == "open" and len(command) >= 3 and command[1] == "-a":
        app_name = command[2]

        # Check custom launcher in ~/Applications first
        custom_app = Path.home() / "Applications" / f"{app_name}.app"
        if custom_app.exists():
            return True

        result = subprocess.run(
            ["open", "-Ra", app_name],
            capture_output=True,
        )
        return result.returncode == 0

    return shutil.which(first) is not None or Path(first).exists()
