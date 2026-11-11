#include <stdio.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_io_expander_tca95xx_16bit.h"
#include "led_strip.h"

// Waveshare ESP32-S3-AUDIO-Board pins
#define I2C_SCL     GPIO_NUM_10
#define I2C_SDA     GPIO_NUM_11
#define I2S_MCLK    GPIO_NUM_12
#define I2S_BCLK    GPIO_NUM_13
#define I2S_LRCK    GPIO_NUM_14
#define I2S_DOUT    GPIO_NUM_16   // ESP32 -> ES8311 (speaker)
#define AMP_EN_PIN  IO_EXPANDER_PIN_NUM_8   // speaker amp enable is on the TCA9555

#define LED_PIN     GPIO_NUM_38   // ring of RGB lights
#define LED_COUNT   7
#define LED_BRIGHT  40            // 0-255, keep low so it isn't blinding

#define SAMPLE_RATE 16000
#define VOLUME      70            // 0-100

// "Hello world" voice clip: 16 kHz, 16-bit, mono (see main/hello.pcm)
extern const uint8_t hello_pcm_start[] asm("_binary_hello_pcm_start");
extern const uint8_t hello_pcm_end[]   asm("_binary_hello_pcm_end");

// Spins a rainbow around the LED ring forever
static void rainbow_task(void *arg)
{
    led_strip_handle_t strip;
    led_strip_config_t strip_cfg = {
        .strip_gpio_num = LED_PIN,
        .max_leds = LED_COUNT,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_RGB,
    };
    led_strip_rmt_config_t rmt_cfg = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
    };
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &strip));

    uint16_t hue = 0;
    while (1) {
        for (int i = 0; i < LED_COUNT; i++) {
            // spread the colors of the rainbow evenly around the ring
            led_strip_set_pixel_hsv(strip, i, (hue + i * 360 / LED_COUNT) % 360, 255, LED_BRIGHT);
        }
        led_strip_refresh(strip);
        hue = (hue + 3) % 360;   // bigger number = faster spin
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void app_main(void)
{
    xTaskCreate(rainbow_task, "rainbow", 4096, NULL, 5, NULL);

    // I2C bus (shared by the ES8311 codec and the TCA9555 IO expander)
    i2c_master_bus_handle_t i2c_bus;
    i2c_master_bus_config_t i2c_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = I2C_SDA,
        .scl_io_num = I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_cfg, &i2c_bus));

    // Turn on the speaker amplifier
    esp_io_expander_handle_t io_expander;
    ESP_ERROR_CHECK(esp_io_expander_new_i2c_tca95xx_16bit(i2c_bus, ESP_IO_EXPANDER_I2C_TCA9555_ADDRESS_000, &io_expander));
    esp_io_expander_set_dir(io_expander, AMP_EN_PIN, IO_EXPANDER_OUTPUT);
    esp_io_expander_set_level(io_expander, AMP_EN_PIN, 1);

    // I2S output to the codec
    i2s_chan_handle_t tx_handle;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &tx_handle, NULL));
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_MCLK,
            .bclk = I2S_BCLK,
            .ws   = I2S_LRCK,
            .dout = I2S_DOUT,
            .din  = I2S_GPIO_UNUSED,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx_handle, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(tx_handle));

    // ES8311 speaker codec
    audio_codec_i2s_cfg_t i2s_if_cfg = { .port = I2S_NUM_1, .tx_handle = tx_handle };
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_if_cfg);
    audio_codec_i2c_cfg_t i2c_if_cfg = { .port = I2C_NUM_0, .addr = ES8311_CODEC_DEFAULT_ADDR, .bus_handle = i2c_bus };
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&i2c_if_cfg);
    const audio_codec_gpio_if_t *gpio_if = audio_codec_new_gpio();
    es8311_codec_cfg_t es8311_cfg = {
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .ctrl_if = ctrl_if,
        .gpio_if = gpio_if,
        .pa_pin = -1,          // amp is controlled through the IO expander above
        .use_mclk = false,
    };
    esp_codec_dev_cfg_t dev_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = es8311_codec_new(&es8311_cfg),
        .data_if = data_if,
    };
    esp_codec_dev_handle_t speaker = esp_codec_dev_new(&dev_cfg);
    esp_codec_dev_sample_info_t fs = {
        .sample_rate = SAMPLE_RATE,
        .channel = 2,
        .bits_per_sample = 16,
    };
    ESP_ERROR_CHECK(esp_codec_dev_open(speaker, &fs));
    esp_codec_dev_set_out_vol(speaker, VOLUME);

    // Convert the mono clip to stereo (same sound on left and right)
    const int16_t *mono = (const int16_t *)hello_pcm_start;
    size_t samples = (hello_pcm_end - hello_pcm_start) / sizeof(int16_t);
    int16_t *stereo = malloc(samples * 2 * sizeof(int16_t));
    for (size_t i = 0; i < samples; i++) {
        stereo[2 * i] = mono[i];
        stereo[2 * i + 1] = mono[i];
    }

    int count = 0;
    while (1) {
        printf("Hello world! (%d)\n", count++);
        esp_codec_dev_write(speaker, stereo, samples * 2 * sizeof(int16_t));
        vTaskDelay(pdMS_TO_TICKS(2000));  // pause 2 seconds between each one
    }
}
