#include "leds.h"

#include <string.h>

#include "app_config.h"
#include "app_shared.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "led_strip.h"
#include "net/telemetry.h"
#include "pins.h"

static const char *TAG = "leds";

typedef struct {
    bool status_dirty;
    led_status_t status;
    bool topic_dirty;
    int topic;
    float confidence;
    bool afterglow;
    int probe_status;          // -1 none
    uint32_t probe_until;
    led_diag_t diag;
    uint32_t diag_until;
} led_req_t;

static led_req_t s_req = {.probe_status = -1};
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static led_stats_t s_stats;
static led_strip_handle_t s_strip;

void leds_set_status(led_status_t s)
{
    taskENTER_CRITICAL(&s_lock);
    s_req.status = s;
    s_req.status_dirty = true;
    taskEXIT_CRITICAL(&s_lock);
}

void leds_set_topic(int topic, float confidence)
{
    taskENTER_CRITICAL(&s_lock);
    s_req.topic = topic;
    s_req.confidence = confidence;
    s_req.topic_dirty = true;
    taskEXIT_CRITICAL(&s_lock);
}

void leds_start_afterglow(void)
{
    taskENTER_CRITICAL(&s_lock);
    s_req.afterglow = true;
    taskEXIT_CRITICAL(&s_lock);
}

void leds_probe(int status, uint32_t duration_ms)
{
    taskENTER_CRITICAL(&s_lock);
    s_req.probe_status = (status >= 0 && status < LS_COUNT) ? status : -1;
    s_req.probe_until = now_ms() + duration_ms;
    taskEXIT_CRITICAL(&s_lock);
}

void leds_diag(led_diag_t pattern, uint32_t duration_ms)
{
    taskENTER_CRITICAL(&s_lock);
    s_req.diag = pattern;
    s_req.diag_until = now_ms() + duration_ms;
    taskEXIT_CRITICAL(&s_lock);
}

void leds_get_stats(led_stats_t *out)
{
    taskENTER_CRITICAL(&s_lock);
    *out = s_stats;
    taskEXIT_CRITICAL(&s_lock);
}

static void config_from_settings(led_config_t *k, const dev_settings_t *st)
{
    k->count = LED_COUNT;
    k->offset = st->led_offset % LED_COUNT;
    k->reverse = st->led_reverse;
    k->condition = st->condition;
    k->reduced_motion = st->reduced_motion;
    k->lights_off = st->lights_off;
    k->max_brightness_pct = st->max_brightness_pct > LED_HARD_MAX_PCT ? LED_HARD_MAX_PCT : st->max_brightness_pct;
    k->afterglow_ms = st->afterglow_ms;
}

static void diag_frame(led_diag_t d, const led_config_t *k, rgb_t *out)
{
    memset(out, 0, sizeof(rgb_t) * k->count);
    uint8_t v = (uint8_t)(255 * k->max_brightness_pct / 100);
    switch (d) {
    case LED_DIAG_LOGICAL_ZERO:
        out[led_logical_to_physical(k, 0)] = (rgb_t){v, v, v};
        break;
    case LED_DIAG_RGB:
        out[led_logical_to_physical(k, 0)] = (rgb_t){v, 0, 0};
        out[led_logical_to_physical(k, 1)] = (rgb_t){0, v, 0};
        out[led_logical_to_physical(k, 2)] = (rgb_t){0, 0, v};
        break;
    case LED_DIAG_ALL_WHITE_LOW:
        for (int i = 0; i < k->count; i++) out[i] = (rgb_t){v / 5, v / 5, v / 5};
        break;
    default:
        break;
    }
}

static void led_task(void *arg)
{
    led_engine_t eng;           // probe engine renders researcher probes separately
    led_engine_t probe_eng;
    led_engine_init(&eng, now_ms());
    led_engine_init(&probe_eng, now_ms());
    rgb_t frame[LED_MAX_PIXELS];
    int active_probe = -1;
    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period = pdMS_TO_TICKS(LED_FRAME_MS);
    int64_t next_slot_us = esp_timer_get_time();

    for (;;) {
        int64_t start_us = esp_timer_get_time();
        uint32_t now = now_ms();
        dev_settings_t st;
        settings_get(&st);
        led_config_t k;
        config_from_settings(&k, &st);

        led_req_t r;
        taskENTER_CRITICAL(&s_lock);
        r = s_req;
        s_req.status_dirty = s_req.topic_dirty = s_req.afterglow = false;
        taskEXIT_CRITICAL(&s_lock);

        if (r.topic_dirty) led_engine_set_topic(&eng, r.topic, r.confidence);
        if (r.status_dirty) led_engine_set_status(&eng, r.status, now);
        if (r.afterglow) led_engine_start_afterglow(&eng, &k, now);

        bool probing = r.probe_status >= 0 && (int32_t)(r.probe_until - now) > 0;
        bool diag = r.diag != LED_DIAG_NONE && (int32_t)(r.diag_until - now) > 0;
        if (diag) {
            diag_frame(r.diag, &k, frame);
        } else if (probing) {
            if (active_probe != r.probe_status) {
                led_engine_init(&probe_eng, now);
                led_engine_set_status(&probe_eng, (led_status_t)r.probe_status, now);
                active_probe = r.probe_status;
            }
            led_engine_render(&probe_eng, &k, now, frame);
        } else {
            active_probe = -1;
            led_engine_render(&eng, &k, now, frame);
        }

        if (s_strip) {
            for (int i = 0; i < k.count; i++) {
                rgb_t c = frame[i];
                if (st.led_grb) led_strip_set_pixel(s_strip, i, c.g, c.r, c.b);
                else led_strip_set_pixel(s_strip, i, c.r, c.g, c.b);
            }
            led_strip_refresh(s_strip);
        }

        int64_t end_us = esp_timer_get_time();
        taskENTER_CRITICAL(&s_lock);
        s_stats.frames++;
        if (start_us - next_slot_us > 5000) s_stats.late_frames++;
        uint32_t render_us = (uint32_t)(end_us - start_us);
        if (render_us > s_stats.max_render_us) s_stats.max_render_us = render_us;
        taskEXIT_CRITICAL(&s_lock);
        next_slot_us += LED_FRAME_MS * 1000;
        if (end_us - next_slot_us > 100000) next_slot_us = end_us;   // resync after a long stall

        vTaskDelayUntil(&last_wake, period);
    }
}

void leds_start(void)
{
    led_strip_config_t strip_cfg = {
        .strip_gpio_num = PIN_LED_DATA,
        .max_leds = LED_COUNT,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_RGB,   // [PRIOR]; runtime led_grb swaps
    };
    led_strip_rmt_config_t rmt_cfg = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
    };
    esp_err_t err = led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &s_strip);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LED strip init failed: %s (continuing without lights)", esp_err_to_name(err));
        s_strip = NULL;
    }
    s_stats.pixel_count = LED_COUNT;
    s_stats.strip_ok = s_strip != NULL;
    xTaskCreatePinnedToCore(led_task, "leds", 4096, NULL, 6, NULL, 1);
}
