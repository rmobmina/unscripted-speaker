#include "board.h"

#include <string.h>

#include "app_config.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev_defaults.h"
#include "esp_io_expander_tca95xx_16bit.h"
#include "esp_log.h"
#include "pins.h"

static const char *TAG = "board";

static i2c_master_bus_handle_t s_i2c;
static esp_io_expander_handle_t s_expander;
static esp_codec_dev_handle_t s_speaker;
static esp_codec_dev_handle_t s_mic;

esp_codec_dev_handle_t board_speaker(void) { return s_speaker; }
esp_codec_dev_handle_t board_mic(void) { return s_mic; }

static void i2c_scan(board_status_t *st)
{
    st->i2c_device_count = 0;
    for (uint8_t a = 0x08; a < 0x78; a++) {
        if (i2c_master_probe(s_i2c, a, 50) == ESP_OK) {
            if (st->i2c_device_count < (int)sizeof(st->i2c_devices)) st->i2c_devices[st->i2c_device_count++] = a;
            if (a == 0x18) st->es8311_found = true;
            if (a == 0x40) st->es7210_found = true;
            if (a == 0x20) st->tca9555_found = true;
            ESP_LOGI(TAG, "I2C device at 0x%02x", a);
        }
    }
}

esp_err_t board_init(board_status_t *st)
{
    memset(st, 0, sizeof(*st));

    i2c_master_bus_config_t i2c_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = PIN_I2C_SDA,
        .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t err = i2c_new_master_bus(&i2c_cfg, &s_i2c);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2C init failed: %s", esp_err_to_name(err));
        return err;
    }
    st->i2c_ok = true;
    i2c_scan(st);

    // Speaker amplifier enable lives on the IO expander.
    err = esp_io_expander_new_i2c_tca95xx_16bit(s_i2c, ESP_IO_EXPANDER_I2C_TCA9555_ADDRESS_000, &s_expander);
    if (err == ESP_OK) {
        esp_io_expander_set_dir(s_expander, (1u << EXPANDER_AMP_EN_PIN), IO_EXPANDER_OUTPUT);
        esp_io_expander_set_level(s_expander, (1u << EXPANDER_AMP_EN_PIN), 1);
        st->expander_ok = true;
    } else {
        ESP_LOGE(TAG, "IO expander init failed: %s (speaker amp stays off)", esp_err_to_name(err));
    }

    // Full-duplex I2S: one port, shared MCLK/BCLK/LRCK, DOUT to ES8311, DIN from ES7210.
    i2s_chan_handle_t tx = NULL, rx = NULL;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;          // silence (not stale audio) on underrun
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = 240;
    err = i2s_new_channel(&chan_cfg, &tx, &rx);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2S channel failed: %s", esp_err_to_name(err));
        return err;
    }
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = PIN_I2S_MCLK,
            .bclk = PIN_I2S_BCLK,
            .ws = PIN_I2S_LRCK,
            .dout = PIN_I2S_DOUT,
            .din = PIN_I2S_DIN,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx, &std_cfg));

    audio_codec_i2s_cfg_t i2s_if_cfg = {.port = I2S_NUM_0, .rx_handle = rx, .tx_handle = tx};
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_if_cfg);
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();

    // ES8311 speaker DAC
    audio_codec_i2c_cfg_t es8311_i2c = {.port = I2C_NUM_0, .addr = ES8311_CODEC_DEFAULT_ADDR, .bus_handle = s_i2c};
    es8311_codec_cfg_t es8311_cfg = {
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .ctrl_if = audio_codec_new_i2c_ctrl(&es8311_i2c),
        .gpio_if = gpio_if,
        .pa_pin = -1,          // amplifier is switched through the expander
        .use_mclk = false,     // as in the previously working firmware [PRIOR]
    };
    esp_codec_dev_cfg_t spk_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = es8311_codec_new(&es8311_cfg),
        .data_if = data_if,
    };
    s_speaker = esp_codec_dev_new(&spk_cfg);

    // ES7210 microphone ADC
    audio_codec_i2c_cfg_t es7210_i2c = {.port = I2C_NUM_0, .addr = ES7210_CODEC_DEFAULT_ADDR, .bus_handle = s_i2c};
    es7210_codec_cfg_t es7210_cfg = {
        .ctrl_if = audio_codec_new_i2c_ctrl(&es7210_i2c),
        .mic_selected = ES7210_MIC_MASK,
    };
    esp_codec_dev_cfg_t mic_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN,
        .codec_if = es7210_codec_new(&es7210_cfg),
        .data_if = data_if,
    };
    s_mic = esp_codec_dev_new(&mic_cfg);

    esp_codec_dev_sample_info_t fs = {
        .sample_rate = AUDIO_SAMPLE_RATE,
        .channel = 2,
        .bits_per_sample = 16,
    };
    if (s_speaker && esp_codec_dev_open(s_speaker, &fs) == ESP_CODEC_DEV_OK) {
        esp_codec_dev_set_out_vol(s_speaker, AUDIO_DEFAULT_VOLUME);
        st->speaker_ok = true;
    } else {
        ESP_LOGE(TAG, "ES8311 speaker codec failed to open");
    }
    if (s_mic && esp_codec_dev_open(s_mic, &fs) == ESP_CODEC_DEV_OK) {
        esp_codec_dev_set_in_gain(s_mic, AUDIO_MIC_GAIN_DB);
        esp_codec_dev_set_in_mute(s_mic, true);   // microphone muted until a press
        st->mic_ok = true;
    } else {
        ESP_LOGE(TAG, "ES7210 microphone ADC failed to open");
    }
    ESP_LOGI(TAG, "board: i2c=%d expander=%d es8311=%d es7210=%d speaker=%d mic=%d", st->i2c_ok,
             st->expander_ok, st->es8311_found, st->es7210_found, st->speaker_ok, st->mic_ok);
    return ESP_OK;
}

esp_err_t board_expander_inputs(uint32_t *levels)
{
    if (!s_expander) return ESP_ERR_INVALID_STATE;
    return esp_io_expander_get_level(s_expander, 0xFFFF, levels);
}
