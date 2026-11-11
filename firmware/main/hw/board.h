// Board bring-up for the Waveshare ESP32-S3-AUDIO-Board: shared I2C bus,
// TCA9555 expander (speaker amp enable), full-duplex I2S, ES8311 DAC and
// ES7210 microphone ADC.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "driver/i2c_master.h"
#include "esp_codec_dev.h"
#include "esp_err.h"

typedef struct {
    bool i2c_ok;
    bool expander_ok;
    bool es8311_found;   // 7-bit 0x18
    bool es7210_found;   // 7-bit 0x40
    bool tca9555_found;  // 7-bit 0x20
    bool speaker_ok;
    bool mic_ok;
    uint8_t i2c_devices[16];
    int i2c_device_count;
} board_status_t;

esp_err_t board_init(board_status_t *st);
esp_codec_dev_handle_t board_speaker(void);
esp_codec_dev_handle_t board_mic(void);
// Reads the 16 expander input lines (bit per pin), for button discovery.
esp_err_t board_expander_inputs(uint32_t *levels);
