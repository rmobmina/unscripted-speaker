// LED ring renderer: pure, deterministic, hardware-independent.
//
// Composes each frame from three layers (protocol Figure 7):
//   Layer 2 STATUS  - always on top; owns motion and the 4 anchor pixels
//   Layer 1 TOPIC   - B3 only, SPEAKING + afterglow, non-anchor pixels only
//   Layer 0 ACCESS  - reduced motion, global brightness cap, lights off
//
// The same file is compiled for the ESP32, for host unit tests, and into the
// software simulator (tools/led_simulator.py), so simulated output is produced
// by exactly the firmware code.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define LED_MAX_PIXELS 32
#define LED_TRANSITION_MS 120u
#define LED_FRAME_MS 20u                // 50 Hz
#define LED_TOPIC_MIN_CONFIDENCE 0.60f

typedef struct { uint8_t r, g, b; } rgb_t;

typedef enum {
    LS_BOOTING = 0,
    LS_IDLE,
    LS_LISTENING,
    LS_PROCESSING,
    LS_SPEAKING,
    LS_ERROR,
    LS_OFFLINE,
    LS_COUNT
} led_status_t;

typedef enum { COND_B1 = 1, COND_B2 = 2, COND_B3 = 3 } led_condition_t;

typedef enum {
    TOPIC_GENERAL = 0,
    TOPIC_REASSURANCE,
    TOPIC_FAMILY,
    TOPIC_ROUTINE,
    TOPIC_RELAXATION,
    TOPIC_COUNT
} topic_t;

typedef struct {
    uint8_t count;              // physical pixels present (1..LED_MAX_PIXELS)
    uint8_t offset;             // physical index of logical 0 (12 o'clock)
    bool reverse;               // physical indices run counter-clockwise
    led_condition_t condition;  // research condition B1/B2/B3
    bool reduced_motion;        // accessibility: steady levels, no movement
    bool lights_off;            // accessibility: ring dark
    uint8_t max_brightness_pct; // global cap, 0..100
    uint32_t afterglow_ms;      // B3 topic afterglow after playback (0 = off)
} led_config_t;

typedef struct {
    led_status_t status;
    uint32_t status_since_ms;
    topic_t topic;
    bool topic_valid;           // a validated topic exists for the current reply
    uint32_t afterglow_until_ms;
    bool transitioning;
    uint32_t transition_start_ms;
    rgb_t prev_frame[LED_MAX_PIXELS];
    rgb_t last_frame[LED_MAX_PIXELS];
} led_engine_t;

extern const rgb_t LED_STATUS_COLORS[LS_COUNT];
extern const rgb_t LED_TOPIC_COLORS[TOPIC_COUNT];

void led_engine_init(led_engine_t *e, uint32_t now_ms);

// Change status. Starts an eased 120 ms cross-fade from the last frame.
// Entering any status other than SPEAKING/IDLE clears the topic layer, so a
// new interaction immediately shows the pure status pattern.
void led_engine_set_status(led_engine_t *e, led_status_t s, uint32_t now_ms);

// Topic from the validated backend response. Confidence below 0.60, NaN, or
// an out-of-range topic falls back to TOPIC_GENERAL.
void led_engine_set_topic(led_engine_t *e, int topic, float confidence);
void led_engine_clear_topic(led_engine_t *e);
void led_engine_start_afterglow(led_engine_t *e, const led_config_t *cfg, uint32_t now_ms);

// Render one frame into out[cfg->count], indexed by PHYSICAL pixel.
void led_engine_render(led_engine_t *e, const led_config_t *cfg, uint32_t now_ms, rgb_t *out);

// --- helpers exposed for tests ---------------------------------------------
int led_logical_to_physical(const led_config_t *cfg, int logical);
bool led_is_anchor(int logical, int count);
// Status brightness level 0..1 for one logical pixel at time t since entry.
float led_status_level(led_status_t s, bool animated, int logical, int count, uint32_t t_ms);
topic_t led_topic_sanitize(int topic, float confidence);
const char *led_status_name(led_status_t s);

#ifdef __cplusplus
}
#endif
