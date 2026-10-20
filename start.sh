#!/usr/bin/env bash
# Linux / macOS: first run sets everything up, then starts the website.
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt && python download_models.py && exec python run.py
