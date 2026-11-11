// Bounded research-event buffer. Events carry NO audio and NO transcript text.
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    TEL_DEVICE_BOOT = 0,
    TEL_WIFI_CONNECTED,
    TEL_WIFI_DISCONNECTED,
    TEL_BACKEND_CONNECTED,
    TEL_BACKEND_UNREACHABLE,
    TEL_BUTTON_PRESSED,
    TEL_BUTTON_RELEASED,
    TEL_BUTTON_LONG_PRESS,
    TEL_STATE_CHANGED,
    TEL_LISTENING_STARTED,
    TEL_LISTENING_ENDED,
    TEL_SPEECH_DETECTED,
    TEL_AUDIO_UPLOAD_STARTED,
    TEL_AUDIO_UPLOAD_COMPLETED,
    TEL_REPLY_RECEIVED,
    TEL_PLAYBACK_STARTED,
    TEL_PLAYBACK_COMPLETED,
    TEL_PLAYBACK_INTERRUPTED,
    TEL_INTERACTION_CANCELLED,
    TEL_PROCESSING_TIMEOUT,
    TEL_ERROR,
    TEL_CONDITION_APPLIED,
    TEL_SETTINGS_APPLIED,
    TEL_STALE_RESPONSE_REJECTED,
    TEL_CAPTURE_STATS,
    TEL_LED_STATS,
    TEL_STATUS_PROBE,
    TEL_MIC_SELFTEST,
    TEL_SESSION_RESET,
    TEL_COUNT
} tel_name_t;

typedef struct {
    uint8_t name;
    uint8_t state;
    uint8_t condition;
    uint32_t t_ms;        // monotonic ms since boot (latency maths)
    int64_t wall_ms;      // backend-synchronised wall clock, 0 if unknown
    uint32_t turn;
    int32_t value;        // duration / metric, meaning depends on the event
    int16_t code;
    char detail[28];      // short non-sensitive label (reason, state names)
} tel_event_t;

void telemetry_init(void);
void telemetry_log(tel_name_t name, uint32_t turn, int32_t value, int code, const char *detail);
// Copies up to max events (oldest first) without removing them.
int telemetry_peek(tel_event_t *out, int max);
void telemetry_drop(int n);
uint32_t telemetry_dropped(void);
const char *telemetry_name(tel_name_t n);
void telemetry_set_wall_offset(int64_t wall_minus_mono_ms);
