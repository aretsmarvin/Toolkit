"""Auto-installer for external forensic tools.

Supported install strategies on macOS
--------------------------------------
homebrew_cask      : brew install --cask <cask>
homebrew_formula   : brew install <formula>
autopsy_macos      : Full Autopsy macOS install:
                     1. Install Homebrew if missing
                     2. Download + install Liberica JDK 8 DMG directly
                        (avoids sudo in terminal — macOS handles the password
                        prompt natively via the .pkg installer GUI)
                     3. Install sleuthkit via Homebrew (C tools only)
                     4. Download sleuthkit-4.14.0.jar directly from GitHub
                        (Autopsy 4.22.1 requires exactly this version)
                     5. Install testdisk + gstreamer via Homebrew
                     6. Download Autopsy ZIP from GitHub
                     7. Extract to ~/Applications/
                     8. Place JAR in ~/Applications/java/ and set
                        TSK_JAVA_LIB_PATH so unix_setup.sh finds it
                     9. Run unix_setup.sh
                    10. Create ~/Applications/Autopsy.app launcher
dmg                : Download .dmg, mount, copy .app to /Applications
pkg                : Download .pkg, run installer
"""
from __future__ import annotations

import logging
import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from PySide6 import QtCore


LOGGER = logging.getLogger(__name__)

_HOMEBREW_INSTALL_URL = (
    "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
)

# Autopsy 4.22.1 unix_setup.sh hard-codes TSK_VERSION=4.14.0.
# We must supply exactly that JAR file, regardless of what Homebrew installs.
_AUTOPSY_VERSION = "4.22.1"
_TSK_VERSION = "4.14.0"  # Must match TSK_VERSION inside unix_setup.sh
_AUTOPSY_ZIP_URL = (
    f"https://github.com/sleuthkit/autopsy/releases/download/"
    f"autopsy-{_AUTOPSY_VERSION}/autopsy-{_AUTOPSY_VERSION}_v2.zip"
)
# Direct download of the exact JAR Autopsy 4.22.1 needs
_TSK_JAR_URL = (
    f"https://github.com/sleuthkit/sleuthkit/releases/download/"
    f"sleuthkit-{_TSK_VERSION}/sleuthkit-{_TSK_VERSION}.jar"
)
# Liberica JDK 8 full DMG — no brew cask, avoids terminal sudo prompts
_LIBERICA_JDK8_DMG_URL = (
    "https://download.bell-sw.com/java/8u432+7/bellsoft-jdk8u432+7-macos-aarch64-full.dmg"
)
_LIBERICA_JDK8_DMG_URL_X64 = (
    "https://download.bell-sw.com/java/8u432+7/bellsoft-jdk8u432+7-macos-amd64-full.dmg"
)


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
                self._dmg_install(self._cfg["url"], self._cfg.get("app_name"))
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
        """Run *cmd*, logging every output line. Raises RuntimeError on non-zero exit."""
        LOGGER.debug("Running: %s", cmd)
        full_env = os.environ.copy()
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

    def _run_with_sudo(self, cmd: list[str], env: dict | None = None) -> None:
        """Run *cmd* with sudo, prompting for a password via a native macOS
        dialog (osascript) so the user never has to look at the terminal.
        """
        # Build the shell command we want to execute as root
        shell_cmd = " ".join(f'\'{c}\'' for c in cmd)
        # osascript pops a macOS password dialog and then runs the command
        apple_script = (
            f'do shell script "{shell_cmd}" '
            f'with administrator privileges '
            f'without altering line endings'
        )
        self.log.emit("Requesting administrator permission (macOS dialog will appear)\u2026")
        result = subprocess.run(
            ["osascript", "-e", apple_script],
            capture_output=True, text=True,
        )
        if result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                self.log.emit(line)
        if result.returncode != 0:
            err = result.stderr.strip() or f"Exit code {result.returncode}"
            raise RuntimeError(f"Privileged command failed: {err}")

    def _ensure_homebrew(self) -> str:
        """Return path to `brew`, installing Homebrew non-interactively if absent."""
        for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
            if Path(candidate).exists():
                return candidate
        found = shutil.which("brew")
        if found:
            return found

        self.log.emit("Homebrew not found — installing Homebrew\u2026")
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
        self.log.emit("Updating Homebrew\u2026")
        self._run([brew, "update"])
        kind = "cask" if cask else "formula"
        self.log.emit(f"Installing {kind} \u2018{name}\u2019 via Homebrew\u2026")
        cmd = [brew, "install"] + (["--cask"] if cask else []) + [name]
        self._run(cmd)

    def _download_file(self, url: str, suffix: str = "") -> str:
        """Download *url* to a named temp file, streaming progress. Returns path."""
        self.log.emit(f"Downloading {url}\u2026")
        req = urllib.request.Request(
            url, headers={"User-Agent": "Toolkit-Installer/1.0"}
        )
        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=suffix, delete=False
            ) as tmp:
                path = tmp.name
                while True:
                    chunk = resp.read(131072)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded / total * 100)
                        self.log.emit(
                            f"  {pct}%  ({downloaded:,} / {total:,} bytes)"
                        )
        self.log.emit(f"Download complete \u2192 {path}")
        return path

    # ------------------------------------------------------------------
    # Autopsy macOS full installer
    # ------------------------------------------------------------------

    def _autopsy_macos_install(self) -> None:
        """Full zero-interaction Autopsy install for macOS.

        Key design decisions
        --------------------
        - Java 8 installed via a direct DMG download (not brew cask) so the
          installer GUI handles any necessary sudo — no terminal prompts.
        - The Sleuth Kit JAR is downloaded *directly* from GitHub at exactly
          the version Autopsy 4.22.1 expects (4.14.0), bypassing Homebrew
          which ships a different version / filename.
        - TSK C-tools are still installed via Homebrew (needed for autopsy CLI).
        - unix_setup.sh is run with TSK_JAVA_LIB_PATH pointing at our JAR
          and JAVA_HOME set correctly.
        """
        brew = self._ensure_homebrew()
        self.log.emit("Updating Homebrew\u2026")
        self._run([brew, "update"])

        # ----------------------------------------------------------------
        # Step 1: Java 8 via direct DMG (native GUI installer — no sudo in terminal)
        # ----------------------------------------------------------------
        self.log.emit("Step 1/6: Installing Liberica JDK 8 (native macOS installer)\u2026")
        import platform
        arch = platform.machine()  # 'arm64' or 'x86_64'
        jdk_url = _LIBERICA_JDK8_DMG_URL if arch == "arm64" else _LIBERICA_JDK8_DMG_URL_X64
        jdk_dmg = self._download_file(jdk_url, suffix=".dmg")
        self._install_dmg_pkg(jdk_dmg, "Liberica JDK 8")
        os.unlink(jdk_dmg)

        java_home = self._find_java_home()
        self.log.emit(f"Java home: {java_home}")

        # ----------------------------------------------------------------
        # Step 2: Sleuth Kit C tools via Homebrew (cli tools, not the JAR)
        # ----------------------------------------------------------------
        self.log.emit("Step 2/6: Installing The Sleuth Kit C tools\u2026")
        self._run([brew, "install", "sleuthkit"])

        # ----------------------------------------------------------------
        # Step 3: Download the exact JAR version Autopsy 4.22.1 needs
        # ----------------------------------------------------------------
        self.log.emit(
            f"Step 3/6: Downloading Sleuth Kit JAR {_TSK_VERSION} "
            "(version required by Autopsy 4.22.1)\u2026"
        )
        jar_path_raw = self._download_file(_TSK_JAR_URL, suffix=".jar")
        # Place it where unix_setup.sh and Autopsy can find it
        java_dir = Path.home() / "Applications" / "java"
        java_dir.mkdir(parents=True, exist_ok=True)
        jar_dest = java_dir / f"sleuthkit-{_TSK_VERSION}.jar"
        shutil.move(jar_path_raw, jar_dest)
        self.log.emit(f"Sleuth Kit JAR placed at: {jar_dest}")

        # ----------------------------------------------------------------
        # Step 4: TestDisk (PhotoRec) + GStreamer
        # ----------------------------------------------------------------
        self.log.emit("Step 4/6: Installing TestDisk / PhotoRec\u2026")
        self._run([brew, "install", "testdisk"])
        self.log.emit("Installing GStreamer\u2026")
        self._run([brew, "install", "gstreamer"])

        # ----------------------------------------------------------------
        # Step 5: Download + extract Autopsy ZIP
        # ----------------------------------------------------------------
        zip_url = self._cfg.get("zip_url", _AUTOPSY_ZIP_URL)
        self.log.emit(f"Step 5/6: Downloading Autopsy {_AUTOPSY_VERSION}\u2026")
        zip_path = self._download_file(zip_url, suffix=".zip")

        install_parent = Path.home() / "Applications"
        install_parent.mkdir(parents=True, exist_ok=True)

        self.log.emit(f"Extracting Autopsy to {install_parent}\u2026")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(install_parent)
        os.unlink(zip_path)

        autopsy_dir = self._find_autopsy_dir(install_parent)
        self.log.emit(f"Autopsy extracted to: {autopsy_dir}")

        # Make scripts executable
        for script_rel in ("unix_setup.sh", "bin/autopsy"):
            s = autopsy_dir / script_rel
            if s.exists():
                s.chmod(s.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # ----------------------------------------------------------------
        # Step 6: Run unix_setup.sh with correct env vars
        # unix_setup.sh checks:
        #   /usr/share/java/sleuthkit-<TSK_VERSION>.jar
        #   /usr/local/share/java/sleuthkit-<TSK_VERSION>.jar
        #   $TSK_JAVA_LIB_PATH   <—— note: NOT TSK_JAR_PATH
        # ----------------------------------------------------------------
        self.log.emit("Step 6/6: Running unix_setup.sh\u2026")
        setup_env = {
            "JAVA_HOME": java_home,
            # The env var name unix_setup.sh actually reads:
            "TSK_JAVA_LIB_PATH": str(jar_dest),
        }
        setup_script = autopsy_dir / "unix_setup.sh"
        self._run([str(setup_script)], env=setup_env)

        # Create the macOS .app launcher
        self.log.emit("Creating Autopsy.app launcher\u2026")
        self._create_autopsy_app(autopsy_dir, java_home)

        self.log.emit("\n\u2705 Autopsy installation complete.")
        self.log.emit(f"   Launch via the Toolkit or: open -a Autopsy")

    def _install_dmg_pkg(
        self, dmg_path: str, label: str
    ) -> None:
        """Mount a DMG, find the .pkg inside, and run it via the macOS
        native installer GUI (which handles its own admin authentication —
        no terminal sudo prompt).
        """
        self.log.emit(f"Mounting {label} DMG\u2026")
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
            pkgs = list(Path(mount_point).glob("*.pkg"))
            if not pkgs:
                raise RuntimeError(f"No .pkg found in {label} DMG.")
            pkg = pkgs[0]
            self.log.emit(
                f"Installing {pkg.name} via macOS native installer\u2026 "
                "(a password dialog will appear)"
            )
            # Use osascript so the dialog appears as a proper macOS window
            # instead of a terminal prompt
            apple_script = (
                f'do shell script "installer -pkg \'{pkg}\' -target /" '
                f'with administrator privileges '
                f'without altering line endings'
            )
            result2 = subprocess.run(
                ["osascript", "-e", apple_script],
                capture_output=True, text=True,
            )
            for line in (result2.stdout + result2.stderr).strip().splitlines():
                if line.strip():
                    self.log.emit(line)
            if result2.returncode != 0:
                raise RuntimeError(
                    f"{label} installer failed (exit {result2.returncode}): "
                    f"{result2.stderr.strip()}"
                )
            self.log.emit(f"{label} installed successfully.")
        finally:
            subprocess.run(
                ["hdiutil", "detach", mount_point, "-quiet"],
                capture_output=True,
            )

    def _find_java_home(self) -> str:
        """Return JAVA_HOME for the installed Liberica JDK 8."""
        result = subprocess.run(
            ["/usr/libexec/java_home", "-v", "1.8"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

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
            "Check that Liberica JDK 8 was installed correctly."
        )

    @staticmethod
    def _find_autopsy_dir(parent: Path) -> Path:
        """Return the autopsy-X.X.X directory inside *parent*."""
        candidates = sorted(parent.glob("autopsy-*"), reverse=True)
        for c in candidates:
            if c.is_dir() and (c / "bin" / "autopsy").exists():
                return c
        for c in candidates:
            if c.is_dir():
                return c
        raise RuntimeError(
            f"Could not find extracted Autopsy directory inside {parent}."
        )

    def _create_autopsy_app(self, autopsy_dir: Path, java_home: str) -> None:
        """Create a minimal macOS .app bundle wrapping the Autopsy launcher."""
        app_path = Path.home() / "Applications" / "Autopsy.app"
        macos_dir = app_path / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True, exist_ok=True)

        plist_text = (
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
        (app_path / "Contents" / "Info.plist").write_text(plist_text)

        launcher_script = (
            "#!/bin/bash\n"
            f'export JAVA_HOME="{java_home}"\n'
            f'export PATH="$JAVA_HOME/bin:$PATH"\n'
            f'exec "{autopsy_dir}/bin/autopsy" "$@"\n'
        )
        launcher = macos_dir / "autopsy-launcher"
        launcher.write_text(launcher_script)
        launcher.chmod(
            launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        LOGGER.info("Autopsy.app created at %s", app_path)

    # ------------------------------------------------------------------
    # DMG / PKG strategies (used by other tools)
    # ------------------------------------------------------------------

    def _dmg_install(self, url: str, app_name: str | None) -> None:
        dmg_path = self._download_file(url, suffix=".dmg")
        self.log.emit("Mounting DMG\u2026")
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
        pkg_path = self._download_file(url, suffix=".pkg")
        self.log.emit("Running installer (password dialog will appear)\u2026")
        apple_script = (
            f'do shell script "installer -pkg \'{pkg_path}\' -target /" '
            f'with administrator privileges '
            f'without altering line endings'
        )
        result = subprocess.run(
            ["osascript", "-e", apple_script],
            capture_output=True, text=True,
        )
        os.unlink(pkg_path)
        if result.returncode != 0:
            raise RuntimeError(
                f"pkg install failed: {result.stderr.strip()}"
            )


# ---------------------------------------------------------------------------
# Public utility
# ---------------------------------------------------------------------------


def is_tool_available(command: list[str]) -> bool:
    """Return True if the tool described by *command* appears to be installed."""
    if not command:
        return False

    first = command[0]

    if first == "open" and len(command) >= 3 and command[1] == "-a":
        app_name = command[2]
        # Check our custom .app wrapper first
        custom_app = Path.home() / "Applications" / f"{app_name}.app"
        if custom_app.exists():
            return True
        result = subprocess.run(
            ["open", "-Ra", app_name], capture_output=True,
        )
        return result.returncode == 0

    return shutil.which(first) is not None or Path(first).exists()
