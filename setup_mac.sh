#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo 'Run this script on a Mac.' >&2
    exit 1
fi
if ! command -v brew >/dev/null 2>&1; then
    echo 'Homebrew is required. Install it from https://brew.sh/ and rerun this script.' >&2
    exit 1
fi

brew install python@3.12 python-tk@3.12 ffmpeg cloudflared
python312="$(brew --prefix python@3.12)/bin/python3.12"
# Homebrew's Python exposes its separately packaged tkinter only to venvs
# that include the brewed site-packages paths.
"$python312" -m venv --system-site-packages .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -c 'import tkinter; print("Tkinter ready")'
chmod +x start_mac.command

echo
echo 'Setup complete. Grant Screen & System Audio Recording and Accessibility to Terminal/Python in macOS Privacy & Security.'
echo 'Set the selected Mac display to the receiving device exact pixel size, then run ./start_mac.command.'
echo 'See README.md for virtual screen and permission instructions.'
