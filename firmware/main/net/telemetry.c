#include "telemetry.h"

#include <string.h>

#include "app_shared.h"
#include "freertos/FreeRTOS.h"

static tel_event_t s_ring[128];
static int s_head, s_count;
static uint32_t s_dropped;
static int64_t s_wall_offset;      // wall_ms = t_ms + offset, 0 if unknown
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

static const char *NAMES[TEL_COUNT] = {
    "device_boot", "wifi_connected", "wifi_disconnected", "backend_connected", "backend_unreachable",
    "button_pressed", "button_released", "button_long_press", "state_changed", "listening_started",
    "listening_ended", "speech_detected", "audio_upload_started", "audio_upload_completed",
    "reply_received", "playback_started", "playback_completed", "playback_interrupted",
    "interaction_cancelled", "processing_timeout", "error", "condition_applied", "settings_applied",
    "stale_response_rejected", "capture_stats", "led_stats", "status_probe", "mic_selftest",
    "session_reset",
};

const char *telemetry_name(tel_name_t n) { return n < TEL_COUNT ? NAMES[n] : "unknown"; }

void telemetry_init(void)
{
    s_head = s_count = 0;
    s_dropped = 0;
}

void telemetry_set_wall_offset(int64_t off)
{
    taskENTER_CRITICAL(&s_lock);
    s_wall_offset = off;
    taskEXIT_CRITICAL(&s_lock);
}

void telemetry_log(tel_name_t name, uint32_t turn, int32_t value, int code, const char *detail)
{
    dev_settings_t st;
    settings_get(&st);
    tel_event_t e = {
        .name = (uint8_t)name,
        .state = (uint8_t)device_state(),
        .condition = (uint8_t)st.condition,
        .t_ms = now_ms(),
        .turn = turn,
        .value = value,
        .code = (int16_t)code,
    };
    if (detail) strlcpy(e.detail, detail, sizeof(e.detail));
    const int cap = (int)(sizeof(s_ring) / sizeof(s_ring[0]));
    taskENTER_CRITICAL(&s_lock);
    e.wall_ms = s_wall_offset ? (int64_t)e.t_ms + s_wall_offset : 0;
    if (s_count == cap) {           // drop oldest
        s_head = (s_head + 1) % cap;
        s_count--;
        s_dropped++;
    }
    s_ring[(s_head + s_count) % cap] = e;
    s_count++;
    taskEXIT_CRITICAL(&s_lock);
}

int telemetry_peek(tel_event_t *out, int max)
{
    const int cap = (int)(sizeof(s_ring) / sizeof(s_ring[0]));
    taskENTER_CRITICAL(&s_lock);
    int n = s_count < max ? s_count : max;
    for (int i = 0; i < n; i++) out[i] = s_ring[(s_head + i) % cap];
    taskEXIT_CRITICAL(&s_lock);
    return n;
}

void telemetry_drop(int n)
{
    const int cap = (int)(sizeof(s_ring) / sizeof(s_ring[0]));
    taskENTER_CRITICAL(&s_lock);
    if (n > s_count) n = s_count;
    s_head = (s_head + n) % cap;
    s_count -= n;
    taskEXIT_CRITICAL(&s_lock);
}

uint32_t telemetry_dropped(void) { return s_dropped; }
