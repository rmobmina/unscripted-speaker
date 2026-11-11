// Wi-Fi speaker: plays audio streamed from the Mac (mac/stream.py) and
// pulses the rainbow lights to the music.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/stream_buffer.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "nvs_flash.h"
#include "mdns.h"
#include "lwip/sockets.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_io_expander_tca95xx_16bit.h"
#include "led_strip.h"
#include "wifi_config.h"

static const char *TAG = "speaker";

// Waveshare ESP32-S3-AUDIO-Board pins
#define I2C_SCL     GPIO_NUM_10
#define I2C_SDA     GPIO_NUM_11
#define I2S_MCLK    GPIO_NUM_12
#define I2S_BCLK    GPIO_NUM_13
#define I2S_LRCK    GPIO_NUM_14
#define I2S_DOUT    GPIO_NUM_16
#define AMP_EN_PIN  IO_EXPANDER_PIN_NUM_8
#define LED_PIN     GPIO_NUM_38
#define LED_COUNT   7

#define SAMPLE_RATE 48000         // must match mac/stream.py
#define VOLUME      70            // 0-100, the Spotify volume slider also works
#define LED_MAX     120           // brightest the lights get on loud parts (0-255)
#define LED_IDLE    15            // brightness when nothing is playing

#define PORT        3333
#define HOSTNAME    "esp-speaker" // the Mac finds the board as esp-speaker.local

#define BUF_SIZE    (64 * 1024)   // ~340 ms of audio to smooth out Wi-Fi hiccups
#define PREFILL     (32 * 1024)   // wait for this much before starting to play
#define CHUNK       2048

// "Speaker ready" voice clip: 48 kHz, 16-bit, mono
extern const uint8_t ready_pcm_start[] asm("_binary_ready_pcm_start");
extern const uint8_t ready_pcm_end[]   asm("_binary_ready_pcm_end");

static StreamBufferHandle_t audio_buf;
static esp_codec_dev_handle_t speaker;
static volatile int music_level;  // 0-255, how loud the music is right now
static EventGroupHandle_t wifi_events;
#define WIFI_CONNECTED BIT0

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
    int bright = LED_IDLE;
    while (1) {
        // jump up instantly on a beat, then fade back down smoothly
        int target = LED_IDLE + music_level * (LED_MAX - LED_IDLE) / 255;
        bright = target > bright ? target : bright - (bright - target) / 8 - 1;
        if (bright < LED_IDLE) {
            bright = LED_IDLE;
        }
        for (int i = 0; i < LED_COUNT; i++) {
            led_strip_set_pixel_hsv(strip, i, (hue + i * 360 / LED_COUNT) % 360, 255, bright);
        }
        led_strip_refresh(strip);
        hue = (hue + (music_level > 20 ? 4 : 1)) % 360;  // spin faster while music plays
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

static void speaker_init(void)
{
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

    i2s_chan_handle_t tx_handle;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    chan_cfg.dma_desc_num = 8;
    chan_cfg.dma_frame_num = 480;
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

    audio_codec_i2s_cfg_t i2s_if_cfg = { .port = I2S_NUM_1, .tx_handle = tx_handle };
    audio_codec_i2c_cfg_t i2c_if_cfg = { .port = I2C_NUM_0, .addr = ES8311_CODEC_DEFAULT_ADDR, .bus_handle = i2c_bus };
    es8311_codec_cfg_t es8311_cfg = {
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .ctrl_if = audio_codec_new_i2c_ctrl(&i2c_if_cfg),
        .gpio_if = audio_codec_new_gpio(),
        .pa_pin = -1,          // amp is controlled through the IO expander above
        .use_mclk = false,
    };
    esp_codec_dev_cfg_t dev_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = es8311_codec_new(&es8311_cfg),
        .data_if = audio_codec_new_i2s_data(&i2s_if_cfg),
    };
    speaker = esp_codec_dev_new(&dev_cfg);
    esp_codec_dev_sample_info_t fs = {
        .sample_rate = SAMPLE_RATE,
        .channel = 2,
        .bits_per_sample = 16,
    };
    ESP_ERROR_CHECK(esp_codec_dev_open(speaker, &fs));
    esp_codec_dev_set_out_vol(speaker, VOLUME);
}

static void say_ready(void)
{
    const int16_t *mono = (const int16_t *)ready_pcm_start;
    size_t samples = (ready_pcm_end - ready_pcm_start) / sizeof(int16_t);
    int16_t stereo[2 * 256];
    for (size_t i = 0; i < samples; i += 256) {
        size_t n = samples - i < 256 ? samples - i : 256;
        for (size_t j = 0; j < n; j++) {
            stereo[2 * j] = stereo[2 * j + 1] = mono[i + j];
        }
        esp_codec_dev_write(speaker, stereo, n * 2 * sizeof(int16_t));
    }
}

// Takes audio out of the buffer and sends it to the speaker
static void play_task(void *arg)
{
    static int16_t chunk[CHUNK / 2];
    bool playing = false;
    while (1) {
        if (!playing) {
            music_level = 0;
            if (xStreamBufferBytesAvailable(audio_buf) < PREFILL) {
                vTaskDelay(pdMS_TO_TICKS(10));
                continue;
            }
            playing = true;
        }
        size_t n = xStreamBufferReceive(audio_buf, chunk, CHUNK, pdMS_TO_TICKS(100));
        n &= ~3;  // whole stereo frames only
        if (n == 0) {
            playing = false;  // ran dry: wait to refill before playing again
            continue;
        }
        esp_codec_dev_write(speaker, chunk, n);

        int peak = 0;
        for (size_t i = 0; i < n / 2; i++) {
            int s = abs(chunk[i]);
            if (s > peak) {
                peak = s;
            }
        }
        music_level = peak >> 7;  // 0-32767 -> 0-255
    }
}

// Accepts the connection from the Mac and fills the buffer with what it sends
static void server_task(void *arg)
{
    static uint8_t rx[4096];
    int listener = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    bind(listener, (struct sockaddr *)&addr, sizeof(addr));
    listen(listener, 1);

    while (1) {
        ESP_LOGI(TAG, "Waiting for the Mac to connect on port %d...", PORT);
        int client = accept(listener, NULL, NULL);
        if (client < 0) {
            continue;
        }
        ESP_LOGI(TAG, "Mac connected, playing!");
        int len;
        while ((len = recv(client, rx, sizeof(rx), 0)) > 0) {
            xStreamBufferSend(audio_buf, rx, len, portMAX_DELAY);
        }
        ESP_LOGI(TAG, "Mac disconnected");
        close(client);
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "Wi-Fi disconnected, retrying... (check the name/password in wifi_config.h)");
        vTaskDelay(pdMS_TO_TICKS(2000));
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = data;
        ESP_LOGI(TAG, "Connected! Board address: " IPSTR "  (or " HOSTNAME ".local)", IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED);
    }
}

static void wifi_init(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    wifi_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL);

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASSWORD,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_ps(WIFI_PS_NONE);  // full speed Wi-Fi, no power saving gaps

    ESP_LOGI(TAG, "Connecting to Wi-Fi \"%s\"...", WIFI_SSID);
    xEventGroupWaitBits(wifi_events, WIFI_CONNECTED, false, true, portMAX_DELAY);

    // Lets the Mac find the board by name instead of an IP address
    mdns_init();
    mdns_hostname_set(HOSTNAME);
    mdns_service_add(NULL, "_espspeaker", "_tcp", PORT, NULL, 0);
}

void app_main(void)
{
    xTaskCreate(rainbow_task, "rainbow", 4096, NULL, 3, NULL);
    speaker_init();
    audio_buf = xStreamBufferCreate(BUF_SIZE, 1);

    wifi_init();
    say_ready();

    xTaskCreate(play_task, "play", 4096, NULL, 6, NULL);
    xTaskCreate(server_task, "server", 4096, NULL, 5, NULL);
}
