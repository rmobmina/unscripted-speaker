#!/bin/zsh
# Usage: ./speak.sh "text to say"     (or just ./speak.sh to re-flash the current saying)
set -e
cd "$(dirname "$0")"
PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
[ -z "$PORT" ] && { echo "Board not found - is it plugged in?"; exit 1; }

export IDF_PATH=~/esp/esp-idf
export IDF_PYTHON_ENV_PATH=~/.espressif/python_env/idf5.5_py3.13_env
export PATH="$IDF_PYTHON_ENV_PATH/bin:$PATH"
. $IDF_PATH/export.sh > /dev/null

if [ -n "$1" ]; then
    echo "Recording: \"$1\""
    say -o main/hello.wav --file-format=WAVE --data-format=LEI16@16000 "$1"
    python -I -c "import wave; w=wave.open('main/hello.wav'); open('main/hello.pcm','wb').write(w.readframes(w.getnframes()))"
    rm main/hello.wav
fi

idf.py -p $PORT flash
echo "Done! The board is now talking."
