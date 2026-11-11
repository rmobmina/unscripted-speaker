// Debounce + press / release / long-press detection. Pure logic: feed it the
// raw pin level every few milliseconds; it returns at most one event per call.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    BTN_EV_NONE = 0,
    BTN_EV_PRESS,     // debounced press edge
    BTN_EV_RELEASE,   // debounced release edge (also emitted after a long press)
    BTN_EV_LONG,      // held continuously for long_ms (fires once per press)
} btn_event_t;

typedef struct {
    uint16_t debounce_ms;
    uint16_t long_ms;
    bool raw;             // last raw sample
    bool stable;          // debounced state (true = pressed)
    uint32_t raw_since;   // when raw last changed
    uint32_t press_ms;    // when the stable press began
    bool long_fired;
    bool initialised;
} btn_logic_t;

void btn_init(btn_logic_t *b, uint16_t debounce_ms, uint16_t long_ms);
btn_event_t btn_update(btn_logic_t *b, bool pressed_raw, uint32_t now_ms);
uint32_t btn_held_ms(const btn_logic_t *b, uint32_t now_ms);

#ifdef __cplusplus
}
#endif
