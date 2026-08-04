#!/bin/zsh
set -e
cd "$(dirname "$0")"
echo "HILLARY STYLE — FIRST-TIME SETUP"
if ! command -v python3 >/dev/null 2>&1; then xcode-select --install || true; exit 1; fi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
chmod +x run.command setup.command
echo "SETUP COMPLETE"
read -r "?Press Enter to close."
