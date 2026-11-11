// Copy to secrets.h (git-ignored) or run:  scripts/configure_device.sh
// NEVER put an OpenAI API key here. The device only knows its own device token.
#pragma once

#define UNS_WIFI_SSID       "YOUR_2_4GHZ_WIFI_NAME"
#define UNS_WIFI_PASSWORD   "YOUR_WIFI_PASSWORD"

// Base URL of the backend as seen FROM THE ESP32. Use your Mac's LAN address
// (e.g. http://192.168.1.23:8000), never localhost / 127.0.0.1: on the device,
// "localhost" is the ESP32 itself. Use https:// for any non-lab deployment.
#define UNS_BACKEND_URL     "http://192.168.1.23:8000"

// Anonymous device identifier and per-device bearer token issued by
// `python -m app.cli provision-device` on the backend.
#define UNS_DEVICE_ID       "dev-01"
#define UNS_DEVICE_TOKEN    "REPLACE_WITH_PROVISIONED_TOKEN"
