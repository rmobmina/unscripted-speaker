#!/bin/zsh
# Streams your Mac's sound to the board. Set your Mac's sound output to "BlackHole 2ch" first.
cd "$(dirname "$0")"
if [ ! -d mac/venv ]; then
    echo "First run: installing Python libraries..."
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m venv mac/venv
    mac/venv/bin/pip install -q sounddevice
fi
mac/venv/bin/python mac/stream.py "$@"
