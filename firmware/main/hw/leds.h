// 50 Hz non-blocking LED task (core 1). Owns the led_engine; other tasks send
// it requests through these thread-safe setters. No delay() in the renderer:
// the task wakes on a fixed 20 ms schedule (vTaskDelayUntil).
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "core/led_engine.h"

typedef enum {
    LED_DIAG_NONE = 0,
    LED_DIAG_LOGICAL_ZERO,   // only the 12 o'clock pixel lit (orientation check)
    LED_DIAG_RGB,            // pixel 0 red, 1 green, 2 blue (colour-order check)
    LED_DIAG_ALL_WHITE_LOW,  // every pixel 20% white (dead-pixel check)
} led_diag_t;

typedef struct {
    uint32_t frames;
    uint32_t late_frames;      // frame started > 5 ms after its slot
    uint32_t max_render_us;
    uint8_t pixel_count;
    bool strip_ok;
} led_stats_t;

void leds_start(void);
void leds_set_status(led_status_t s);
void leds_set_topic(int topic, float confidence);
void leds_start_afterglow(void);
void leds_probe(int status, uint32_t duration_ms);   // status < 0 ends the probe
void leds_diag(led_diag_t pattern, uint32_t duration_ms);
void leds_get_stats(led_stats_t *out);
