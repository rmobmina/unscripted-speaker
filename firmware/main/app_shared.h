// Types and functions shared between the firmware tasks.
//
// Task layout (protocol Figure 4):
//   core 1: controller (state machine owner), audio task, LED task (50 Hz)
//   core 0: Wi-Fi/lwIP, config+telemetry task, per-turn HTTP workers
//   esp_timer: button sampling every 5 ms
// Only the controller task mutates the interaction state; everything else
// talks to it through ctrl_post*() (a bounded FreeRTOS queue).
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "core/led_engine.h"
#include "core/sm.h"

uint32_t now_ms(void);

// ---- runtime settings (from the researcher/caregiver dashboard) -------------
typedef struct {
    uint32_t version;
    led_condition_t condition;
    bool reduced_motion;
    bool lights_off;
    uint8_t max_brightness_pct;
    uint8_t volume;
    uint8_t led_offset;
    bool led_reverse;
    bool led_grb;               // swap R/G if the ring is GRB-ordered
    uint32_t afterglow_ms;
    uint32_t silence_endpoint_ms;
    bool endpoint_while_held;
    float vad_abs_dbfs;
    float vad_snr_db;
    uint32_t session_reset_seq;
} dev_settings_t;

void settings_defaults(dev_settings_t *s);
void settings_get(dev_settings_t *out);
void settings_apply(const dev_settings_t *in);   // persists to NVS
void settings_load(void);

// ---- controller mailbox ------------------------------------------------------
typedef enum {
    MSG_SM = 0,          // state-machine event
    MSG_REPLY_META,      // validated reply metadata (topic etc.) for a turn
    MSG_SETTINGS,        // settings changed (condition, accessibility...)
    MSG_STATUS_PROBE,    // researcher status probe (light only)
    MSG_LED_DIAG,        // LED diagnostic pattern
} ctrl_msg_type_t;

typedef struct {
    ctrl_msg_type_t type;
    sm_event_t ev;
    int topic;
    float topic_confidence;
    int probe_status;          // led_status_t, -1 = end probe
    uint32_t probe_ms;
    int diag_pattern;
} ctrl_msg_t;

void ctrl_post(const ctrl_msg_t *m);
void ctrl_post_event(sm_event_type_t type, uint32_t turn, int code);

// ---- turn bookkeeping ----------------------------------------------------------
uint32_t active_turn(void);                  // turn the state machine is on (0 = none)
void turn_id_string(uint32_t turn, char *out, int cap);
void session_get(char *out, int cap);
void session_set(const char *sid);
void session_clear(void);
dev_state_t device_state(void);
const char *device_boot_id(void);
