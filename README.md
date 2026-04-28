# Toolkit GUI

Toolkit is a macOS-focused digital forensics launcher with a modern Qt-based GUI.

## Features

- Configurable branding (name, logo, icon) via `src/config.json`.
- Plugin architecture for custom Python-based forensic tools.
- External tool integration (e.g. Autopsy) driven from config, including an update action.
- Demo disk-imaging plugin that wraps `dd` using a worker thread to keep the GUI responsive.
- Structured logging to `~/.toolkit/logs/toolkit.log`.

## Running from source

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

# Start the GUI
toolkit-gui
```

## Building a macOS binary (PyInstaller)

The repository includes a simple helper script for building a standalone macOS binary using PyInstaller:

```bash
./build_mac_app.sh
```

This produces a self-contained binary under `dist/Toolkit` that you can distribute to other analysts without requiring them to create a virtual environment or install Python dependencies.
