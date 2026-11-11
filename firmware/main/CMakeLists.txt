idf_component_register(
    SRCS "app_main.c"
         "core/sm.c" "core/button_logic.c" "core/led_engine.c" "core/vad.c" "core/byte_ring.c"
         "hw/board.c" "hw/audio.c" "hw/leds.c" "hw/button.c"
         "net/wifi.c" "net/backend.c" "net/telemetry.c"
    INCLUDE_DIRS "." "../config"
    EMBED_FILES "assets/clip_not_understood.pcm" "assets/clip_no_speech.pcm" "assets/clip_error.pcm"
                "assets/clip_timeout.pcm" "assets/clip_offline.pcm"
    PRIV_REQUIRES esp_driver_i2s esp_driver_gpio esp_driver_i2c esp_timer esp_wifi esp_netif esp_event
                  nvs_flash esp_http_client json mbedtls esp_psram esp_hw_support)
if(NOT EXISTS "${CMAKE_CURRENT_LIST_DIR}/secrets.h")
    message(FATAL_ERROR "firmware/main/secrets.h is missing. Run scripts/configure_device.sh (see docs/SETUP.md).")
endif()
