#include "button_logic.h"

void btn_init(btn_logic_t *b, uint16_t debounce_ms, uint16_t long_ms)
{
    b->debounce_ms = debounce_ms;
    b->long_ms = long_ms;
    b->raw = false;
    b->stable = false;
    b->raw_since = 0;
    b->press_ms = 0;
    b->long_fired = false;
    b->initialised = false;
}

btn_event_t btn_update(btn_logic_t *b, bool raw, uint32_t now)
{
    if (!b->initialised) {
        // A button already held at boot is ignored until it is released.
        b->initialised = true;
        b->raw = raw;
        b->stable = raw;
        b->raw_since = now;
        b->long_fired = raw;   // suppress LONG for a press that began before boot
        b->press_ms = now;
        return BTN_EV_NONE;
    }
    if (raw != b->raw) {
        b->raw = raw;
        b->raw_since = now;
    }
    if (b->raw != b->stable && (uint32_t)(now - b->raw_since) >= b->debounce_ms) {
        b->stable = b->raw;
        if (b->stable) {
            b->press_ms = b->raw_since;    // time of the first edge of the press
            b->long_fired = false;
            return BTN_EV_PRESS;
        }
        return BTN_EV_RELEASE;
    }
    if (b->stable && !b->long_fired && (uint32_t)(now - b->press_ms) >= b->long_ms) {
        b->long_fired = true;
        return BTN_EV_LONG;
    }
    return BTN_EV_NONE;
}

uint32_t btn_held_ms(const btn_logic_t *b, uint32_t now)
{
    return b->stable ? (uint32_t)(now - b->press_ms) : 0;
}
