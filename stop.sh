#!/bin/zsh
# Erases the board so it stops talking. Run ./speak.sh to start it again.
set -e
PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
[ -z "$PORT" ] && { echo "Board not found - is it plugged in?"; exit 1; }
~/.espressif/python_env/idf5.5_py3.13_env/bin/python -m esptool --chip esp32s3 -p $PORT erase_flash
echo "Stopped. The board is erased and silent."
