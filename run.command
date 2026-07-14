#!/bin/bash
cd "$(dirname "$0")"
# Prefer the project virtualenv (where the deps are installed); fall back to
# system python3 so a fresh checkout without a .venv still runs.
if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python main.py
fi
exec python3 main.py
