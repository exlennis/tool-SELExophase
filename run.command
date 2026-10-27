#!/bin/zsh
# Resolve the tool folder even when launched from Finder or another directory.
cd -- "$(dirname -- "$0")" || exit 1

if [[ ! -x .venv/bin/python ]]; then
    printf 'The Python environment is missing. Follow README.md to rebuild .venv.\n'
    read -r '?Press Return to close.'
    exit 1
fi

# Start the terminal app without writing Python bytecode files.
exec .venv/bin/python -B SELFexophase.py
