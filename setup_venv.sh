#!/bin/bash
# One-command setup for Team Praann - macOS/Linux
# Run: bash setup_venv.sh
set -e
python3 --version
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo ""
echo "Setup done. Run: source .venv/bin/activate"
