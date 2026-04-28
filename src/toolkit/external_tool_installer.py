"""Auto-installer for external forensic tools.

Supported install strategies on macOS
--------------------------------------
homebrew_cask       : brew install --cask <cask>
homebrew_formula    : brew install <formula>
autopsy_macos       : Full Autopsy macOS install (Java 8 → TSK → JAR → Autopsy)
dmg                 : Download .dmg, mount, copy .app to /Applications
pkg                 : Download .pkg, run via osascript (no terminal sudo)
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

from PySide6 import QtCore

from .net_utils import urlopen

LOGGER = logging.getLogger(__name__)

_HOMEBREW_INSTALL_URL = (
    "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
)

_AUTOPSY_VERSION = "4.22.1"
_TSK_VERSION = "4.14.0"  # Must match TSK_VERSION hard-coded in unix_setup.sh

_AUTOPSY_ZIP_URL = (
    f"https://github.com/sleuthkit/autopsy/releases/download/"
    f"autopsy-{_AUTOPSY_VERSION}/autopsy-{_AUTOPSY_VERSION}_v2.zip"
)
_TSK_JAR_URL = (
    f"https://github.com/sleuthkit/sleuthkit/releases/download/"
    f"sleuthkit-{_TSK_VERSION}/sleuthkit-{_TSK_VERSION}.jar"
)
_LIBERICA_JDK8_BASE = "https://download.bell-sw.com/java/8u432+7"
_LIBERICA_JDK8_URLS = {
    "arm64": f"{_LIBERICA_JDK8_BASE}/bellsoft-jdk8u432+7-macos-aarch64-full.dmg",
    "x86_64": f"{_LIBERICA_JDK8_BASE}/bellsoft-jdk8u432+7-macos-amd64-full.dmg",
}


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
    # Subprocess helpers
    # ------------------------------------------------------------------

    def _run(self, cmd: list[str], env: dict | None = None) -> None:
        """Run *cmd*, logging every output line. Raises RuntimeError on failure."""
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
                stripped = line.rstrip()
                if stripped:
                    LOGGER.debug(stripped)
                    self.log.emit(stripped)
            rc = proc.wait()

        if rc != 0:
            raise RuntimeError(f"Command exited with code {rc}: {cmd}")

    @staticmethod
    def _osascript_sudo(shell_cmd: str) -> None:
        """Run *shell_cmd* with administrator privileges via a native macOS
        password dialog (osascript). Never prompts in the terminal.
        """
        apple_script = (
            f'do shell script "{shell_cmd}" '
            'with administrator privileges '
            'without altering line endings'
        )
        result = subprocess.run(
            ["osascript", "-e", apple_script],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or f"exit code {result.returncode}"
            raise RuntimeError(f"Privileged command failed: {err}")

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _download_file(self, url: str, suffix: str = "") -> str:
        """Download *url* using the certifi SSL context. Returns temp file path."""
        self.log.emit(f"Downloading {url}\u2026")
        with urlopen(url) as resp:
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
                        self.log.emit(f"  {pct}%  ({downloaded:,} / {total:,} bytes)")
        self.log.emit(f"Download complete \u2192 {path}")
        return path

    # ------------------------------------------------------------------
    # Homebrew
    # ------------------------------------------------------------------

    def _ensure_homebrew(self) -> str:
        for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
            if Path(candidate).exists():
                return candidate
        found = shutil.which("brew")
        if found:
            return found

        self.log.emit("Homebrew not found — installing Homebrew\u2026")
        script_path = self._download_file(_HOMEBREW_INSTALL_URL, suffix=".sh")
        os.chmod(script_path, 0o755)
        self._run(["/bin/bash", script_path])
        os.unlink(script_path)

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
        self._run([brew, "install"] + (["--cask"] if cask else []) + [name])

    # ------------------------------------------------------------------
    # Autopsy macOS full installer
    # ------------------------------------------------------------------

    def _autopsy_macos_install(self) -> None:
        brew = self._ensure_homebrew()
        self.log.emit("Updating Homebrew\u2026")
        self._run([brew, "update"])

        # Step 1: Java 8 via direct DMG download + native pkg installer
        self.log.emit("Step 1/6: Installing Liberica JDK 8\u2026")
        arch = platform.machine()  # 'arm64' or 'x86_64'
        jdk_url = _LIBERICA_JDK8_URLS.get(arch, _LIBERICA_JDK8_URLS["x86_64"])
        jdk_dmg = self._download_file(jdk_url, suffix=".dmg")
        self._install_dmg_pkg(jdk_dmg, "Liberica JDK 8")
        os.unlink(jdk_dmg)

        java_home = self._find_java_home()
        self.log.emit(f"Java home: {java_home}")

        # Step 2: Sleuth Kit C tools
        self.log.emit("Step 2/6: Installing The Sleuth Kit C tools\u2026")
        self._run([brew, "install", "sleuthkit"])

        # Step 3: Exact JAR version Autopsy 4.22.1 requires (4.14.0)
        self.log.emit(
            f"Step 3/6: Downloading Sleuth Kit JAR {_TSK_VERSION}\u2026"
        )
        jar_tmp = self._download_file(_TSK_JAR_URL, suffix=".jar")
        java_dir = Path.home() / "Applications" / "java"
        java_dir.mkdir(parents=True, exist_ok=True)
        jar_dest = java_dir / f"sleuthkit-{_TSK_VERSION}.jar"
        shutil.move(jar_tmp, jar_dest)
        self.log.emit(f"JAR placed at: {jar_dest}")

        # Step 4: TestDisk + GStreamer
        self.log.emit("Step 4/6: Installing TestDisk / PhotoRec\u2026")
        self._run([brew, "install", "testdisk"])
        self.log.emit("Installing GStreamer\u2026")
        self._run([brew, "install", "gstreamer"])

        # Step 5: Download + extract Autopsy ZIP
        zip_url = self._cfg.get("zip_url", _AUTOPSY_ZIP_URL)
        self.log.emit(f"Step 5/6: Downloading Autopsy {_AUTOPSY_VERSION}\u2026")
        zip_path = self._download_file(zip_url, suffix=".zip")
        install_parent = Path.home() / "Applications"
        install_parent.mkdir(parents=True, exist_ok=True)
        self.log.emit(f"Extracting to {install_parent}\u2026")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(install_parent)
        os.unlink(zip_path)

        autopsy_dir = self._find_autopsy_dir(install_parent)
        self.log.emit(f"Autopsy extracted to: {autopsy_dir}")

        for script_rel in ("unix_setup.sh", "bin/autopsy"):
            s = autopsy_dir / script_rel
            if s.exists():
                s.chmod(s.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Step 6: Run unix_setup.sh with correct env vars
        self.log.emit("Step 6/6: Running unix_setup.sh\u2026")
        self._run(
            [str(autopsy_dir / "unix_setup.sh")],
            env={
                "JAVA_HOME": java_home,
                "TSK_JAVA_LIB_PATH": str(jar_dest),  # exact var name unix_setup.sh checks
            },
        )

        self.log.emit("Creating Autopsy.app launcher\u2026")
        self._create_autopsy_app(autopsy_dir, java_home)

        self.log.emit("\n\u2705 Autopsy installation complete.")
        self.log.emit("   Use the Launch button or: open -a Autopsy")

    def _install_dmg_pkg(self, dmg_path: str, label: str) -> None:
        """Mount *dmg_path*, find the .pkg inside, install via osascript
        so the password prompt is a native macOS dialog (not the terminal).
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
            pkg = str(pkgs[0])
            self.log.emit(
                f"Installing {Path(pkg).name} — a password dialog will appear\u2026"
            )
            self._osascript_sudo(f"installer -pkg '{pkg}' -target /")
            self.log.emit(f"{label} installed.")
        finally:
            subprocess.run(
                ["hdiutil", "detach", mount_point, "-quiet"],
                capture_output=True,
            )

    def _find_java_home(self) -> str:
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
        for c in sorted(parent.glob("autopsy-*"), reverse=True):
            if c.is_dir() and (c / "bin" / "autopsy").exists():
                return c
        for c in sorted(parent.glob("autopsy-*"), reverse=True):
            if c.is_dir():
                return c
        raise RuntimeError(
            f"Could not find extracted Autopsy directory inside {parent}."
        )

    def _create_autopsy_app(self, autopsy_dir: Path, java_home: str) -> None:
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
    # Generic DMG / PKG strategies
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
        self.log.emit("Running installer — a password dialog will appear\u2026")
        self._osascript_sudo(f"installer -pkg '{pkg_path}' -target /")
        os.unlink(pkg_path)


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
        custom_app = Path.home() / "Applications" / f"{app_name}.app"
        if custom_app.exists():
            return True
        return subprocess.run(
            ["open", "-Ra", app_name], capture_output=True
        ).returncode == 0
    return shutil.which(first) is not None or Path(first).exists()
