#include "led_engine.h"

#include <math.h>
#include <string.h>

#define PI_F 3.14159265f

// Protocol Table 3 (status) and Table 4 (topic) colours.
const rgb_t LED_STATUS_COLORS[LS_COUNT] = {
    [LS_BOOTING]    = {255, 217, 168},
    [LS_IDLE]       = {255, 217, 168},
    [LS_LISTENING]  = {47, 107, 255},
    [LS_PROCESSING] = {255, 163, 26},
    [LS_SPEAKING]   = {31, 191, 107},
    [LS_ERROR]      = {255, 111, 97},
    [LS_OFFLINE]    = {140, 140, 154},
};

const rgb_t LED_TOPIC_COLORS[TOPIC_COUNT] = {
    [TOPIC_GENERAL]     = {242, 228, 201},
    [TOPIC_REASSURANCE] = {60, 199, 180},
    [TOPIC_FAMILY]      = {229, 154, 181},
    [TOPIC_ROUTINE]     = {143, 184, 232},
    [TOPIC_RELAXATION]  = {180, 155, 224},
};

// Brightness constants (fraction of the user's max brightness).
#define LVL_IDLE        0.10f
#define LVL_OFFLINE     0.08f
#define LVL_STATIC      0.70f   // B1 and reduced motion: every active state
#define LVL_PROC_BASE   0.08f
#define LVL_ERROR_HOLD  0.20f
#define LVL_AFTERGLOW   0.35f

#define LISTEN_PERIOD_MS   4000u
#define PROCESS_PERIOD_MS  2000u
#define SPEAK_PERIOD_MS    1200u
#define ERROR_PULSE_MS      600u
#define ERROR_PULSES          2u

const char *led_status_name(led_status_t s)
{
    static const char *names[LS_COUNT] = {"BOOTING", "IDLE", "LISTENING", "PROCESSING",
                                          "SPEAKING", "ERROR", "OFFLINE"};
    return (s >= 0 && s < LS_COUNT) ? names[s] : "?";
}

static float clamp01(float x) { return x < 0.f ? 0.f : (x > 1.f ? 1.f : x); }

// Cosine ease, 0..1 -> 0..1
static float ease(float x) { return 0.5f - 0.5f * cosf(PI_F * clamp01(x)); }

void led_engine_init(led_engine_t *e, uint32_t now_ms)
{
    memset(e, 0, sizeof(*e));
    e->status = LS_BOOTING;
    e->status_since_ms = now_ms;
    e->topic = TOPIC_GENERAL;
}

topic_t led_topic_sanitize(int topic, float confidence)
{
    if (topic < 0 || topic >= TOPIC_COUNT) return TOPIC_GENERAL;
    if (!(confidence >= LED_TOPIC_MIN_CONFIDENCE) || confidence > 1.0f) return TOPIC_GENERAL; // NaN-safe
    return (topic_t)topic;
}

void led_engine_set_topic(led_engine_t *e, int topic, float confidence)
{
    e->topic = led_topic_sanitize(topic, confidence);
    e->topic_valid = true;
}

void led_engine_clear_topic(led_engine_t *e)
{
    e->topic = TOPIC_GENERAL;
    e->topic_valid = false;
    e->afterglow_until_ms = 0;
}

void led_engine_start_afterglow(led_engine_t *e, const led_config_t *cfg, uint32_t now_ms)
{
    if (cfg->afterglow_ms > 0 && e->topic_valid) {
        e->afterglow_until_ms = now_ms + cfg->afterglow_ms;
        if (e->afterglow_until_ms == 0) e->afterglow_until_ms = 1;
    }
}

void led_engine_set_status(led_engine_t *e, led_status_t s, uint32_t now_ms)
{
    if (s == e->status) return;
    memcpy(e->prev_frame, e->last_frame, sizeof(e->prev_frame));
    e->transitioning = true;
    e->transition_start_ms = now_ms;
    e->status = s;
    e->status_since_ms = now_ms;
    if (s != LS_SPEAKING && s != LS_IDLE) {
        // New interaction / error / offline: status pattern only.
        led_engine_clear_topic(e);
    }
    if (s == LS_SPEAKING) e->afterglow_until_ms = 0;
}

int led_logical_to_physical(const led_config_t *cfg, int logical)
{
    int n = cfg->count;
    if (n <= 0) return 0;
    logical = ((logical % n) + n) % n;
    int step = cfg->reverse ? -logical : logical;
    return (((int)cfg->offset + step) % n + n) % n;
}

bool led_is_anchor(int logical, int count)
{
    if (count < 4) return true;   // too few pixels for a topic layer
    for (int k = 0; k < 4; k++) {
        // nearest pixel to 0, 90, 180, 270 degrees (round half up)
        int a = (int)floorf((float)(k * count) / 4.0f + 0.5f) % count;
        if (a == logical) return true;
    }
    return false;
}

float led_status_level(led_status_t s, bool animated, int logical, int count, uint32_t t)
{
    switch (s) {
    case LS_BOOTING:
    case LS_IDLE:
        return LVL_IDLE;
    case LS_OFFLINE:
        return logical == 0 ? LVL_OFFLINE : 0.f;
    default:
        break;
    }
    if (!animated) return LVL_STATIC;

    switch (s) {
    case LS_LISTENING: {
        // Breathing 25-100 %, starts at the peak so the change is obvious at once.
        float ph = (float)(t % LISTEN_PERIOD_MS) / (float)LISTEN_PERIOD_MS;
        return 0.25f + 0.75f * (0.5f + 0.5f * cosf(2.f * PI_F * ph));
    }
    case LS_SPEAKING: {
        float ph = (float)(t % SPEAK_PERIOD_MS) / (float)SPEAK_PERIOD_MS;
        return 0.60f + 0.40f * (0.5f + 0.5f * cosf(2.f * PI_F * ph));
    }
    case LS_PROCESSING: {
        // Comet: head moves clockwise one lap per 2 s with a cosine tail.
        float n = (float)count;
        float head = fmodf((float)t / (float)PROCESS_PERIOD_MS * n, n);
        float d = fmodf(head - (float)logical + n, n);  // distance behind the head
        float tail = n * 0.375f;
        if (tail < 1.5f) tail = 1.5f;
        float lvl = LVL_PROC_BASE;
        if (d <= tail) {
            lvl = LVL_PROC_BASE + (1.f - LVL_PROC_BASE) * (0.5f + 0.5f * cosf(PI_F * d / tail));
        } else if (d > n - 1.f) {
            // pixel just ahead of the head fades in for sub-pixel smoothness
            float f = d - (n - 1.f);
            lvl = LVL_PROC_BASE + (1.f - LVL_PROC_BASE) * ease(f);
        }
        return lvl;
    }
    case LS_ERROR: {
        if (t < ERROR_PULSE_MS * ERROR_PULSES) {
            float ph = (float)(t % ERROR_PULSE_MS) / (float)ERROR_PULSE_MS;
            float sn = sinf(PI_F * ph);
            return LVL_ERROR_HOLD + (1.f - LVL_ERROR_HOLD) * sn * sn;
        }
        return LVL_ERROR_HOLD;
    }
    default:
        return LVL_IDLE;
    }
}

static rgb_t scale(rgb_t c, float level)
{
    level = clamp01(level);
    rgb_t o = {(uint8_t)lrintf(c.r * level), (uint8_t)lrintf(c.g * level), (uint8_t)lrintf(c.b * level)};
    return o;
}

static rgb_t lerp(rgb_t a, rgb_t b, float f)
{
    f = clamp01(f);
    rgb_t o = {(uint8_t)lrintf(a.r + (b.r - a.r) * f), (uint8_t)lrintf(a.g + (b.g - a.g) * f),
               (uint8_t)lrintf(a.b + (b.b - a.b) * f)};
    return o;
}

void led_engine_render(led_engine_t *e, const led_config_t *cfg, uint32_t now, rgb_t *out)
{
    int n = cfg->count;
    if (n < 1) return;
    if (n > LED_MAX_PIXELS) n = LED_MAX_PIXELS;

    uint8_t cap_pct = cfg->max_brightness_pct > 100 ? 100 : cfg->max_brightness_pct;
    float cap = (float)cap_pct / 100.f;
    bool animated = cfg->condition != COND_B1 && !cfg->reduced_motion;
    bool topic_layer = cfg->condition == COND_B3 && e->topic_valid;
    uint32_t t = now - e->status_since_ms;
    rgb_t status_rgb = LED_STATUS_COLORS[e->status];
    rgb_t topic_rgb = LED_TOPIC_COLORS[e->topic];

    bool afterglow = topic_layer && e->status == LS_IDLE && e->afterglow_until_ms != 0 &&
                     (int32_t)(e->afterglow_until_ms - now) > 0;
    float glow_frac = 0.f;
    if (afterglow && cfg->afterglow_ms > 0) {
        glow_frac = (float)(e->afterglow_until_ms - now) / (float)cfg->afterglow_ms;
        glow_frac = ease(glow_frac);   // eased fade-out
    }

    rgb_t frame[LED_MAX_PIXELS];
    for (int i = 0; i < n; i++) {
        float lvl = led_status_level(e->status, animated, i, n, t);
        rgb_t px = scale(status_rgb, lvl);                 // layer 2: status
        bool anchor = led_is_anchor(i, n);
        if (!anchor && topic_layer) {                       // layer 1: topic
            if (e->status == LS_SPEAKING) {
                px = scale(topic_rgb, lvl);                 // status owns motion
            } else if (afterglow) {
                px = lerp(px, scale(topic_rgb, LVL_AFTERGLOW), glow_frac);
            }
        }
        frame[led_logical_to_physical(cfg, i)] = px;
    }

    // Eased cross-fade from the previous status.
    if (e->transitioning) {
        uint32_t dt = now - e->transition_start_ms;
        if (dt >= LED_TRANSITION_MS) {
            e->transitioning = false;
        } else {
            float f = ease((float)dt / (float)LED_TRANSITION_MS);
            for (int p = 0; p < n; p++) frame[p] = lerp(e->prev_frame[p], frame[p], f);
        }
    }
    memcpy(e->last_frame, frame, sizeof(rgb_t) * (size_t)n);  // pre-cap, for fades

    // Layer 0: accessibility / electrical limits.
    for (int p = 0; p < n; p++) {
        out[p] = cfg->lights_off ? (rgb_t){0, 0, 0} : scale(frame[p], cap);
    }
}
