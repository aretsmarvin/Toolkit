#!/usr/bin/env bash
set -euo pipefail

# Simple helper script to build a standalone macOS binary using PyInstaller.
# Requires Python 3.11+ and will create a temporary virtual environment
# for the build process only.

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install . pyinstaller

pyinstaller \
  --name Toolkit \
  --windowed \
  --onefile \
  --noconfirm \
  src/toolkit/gui/main.py

echo "Binary built under dist/Toolkit"
