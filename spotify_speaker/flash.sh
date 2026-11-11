#!/bin/zsh
# Builds the speaker code and puts it on the board.
set -e
cd "$(dirname "$0")"
PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
[ -z "$PORT" ] && { echo "Board not found - is it plugged in?"; exit 1; }
if grep -q YOUR_WIFI_NAME main/wifi_config.h; then
    echo "Put your Wi-Fi name and password in main/wifi_config.h first!"; exit 1
fi

export IDF_PATH=~/esp/esp-idf
export IDF_PYTHON_ENV_PATH=~/.espressif/python_env/idf5.5_py3.13_env
export PATH="$IDF_PYTHON_ENV_PATH/bin:$PATH"
. $IDF_PATH/export.sh > /dev/null

[ -f sdkconfig ] || idf.py set-target esp32s3
idf.py -p $PORT flash monitor
