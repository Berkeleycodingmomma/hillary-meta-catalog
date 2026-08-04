#!/bin/zsh
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then echo "Run setup.command first."; read -r "?Press Enter to close."; exit 1; fi
source .venv/bin/activate
python amazon_meta_sync.py
STATUS=$?
read -r "?Press Enter to close."
exit $STATUS
