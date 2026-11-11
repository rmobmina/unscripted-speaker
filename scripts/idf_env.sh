# Source this to get ESP-IDF v5.5 on PATH:  . scripts/idf_env.sh
export IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"
export IDF_PYTHON_ENV_PATH="${IDF_PYTHON_ENV_PATH:-$HOME/.espressif/python_env/idf5.5_py3.13_env}"
export PATH="$IDF_PYTHON_ENV_PATH/bin:$PATH"
. "$IDF_PATH/export.sh" >/dev/null
