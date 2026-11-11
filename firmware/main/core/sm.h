// Device interaction state machine (protocol Figure 6). Pure and synchronous:
// sm_handle() takes one event and returns the side effects ("actions") the
// firmware must perform. It never blocks, allocates, or touches hardware, so
// every transition is unit-tested on the host (firmware/test).
//
// Interaction model (see docs/ARCHITECTURE.md, "Button semantics"):
//   * Every press-down starts a NEW listening turn immediately (from IDLE,
//     PROCESSING, SPEAKING or ERROR). Pressing during processing/speech
//     interrupts and discards the previous turn.
//   * Hold-to-talk: releasing after speaking submits the recording.
//   * Tap-to-talk: a release within tap_ms with no speech keeps listening
//     hands-free; it ends after silence_endpoint_ms of silence following
//     speech, or on the next press+release.
//   * Long press (2 s): cancels and discards, unless the person is talking
//     while holding (then holding is just hold-to-talk). Audio is only
//     uploaded on RELEASE / endpoint, never on press-down, so a cancelling
//     hold can never transmit a partial request.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    ST_BOOTING = 0,
    ST_IDLE,
    ST_LISTENING,
    ST_PROCESSING,
    ST_SPEAKING,
    ST_ERROR,
    ST_OFFLINE,
    ST_COUNT
} dev_state_t;

typedef enum {
    SM_ERR_NONE = 0,
    SM_ERR_NO_SPEECH,        // nothing heard (device-side)
    SM_ERR_NOT_UNDERSTOOD,   // backend STT returned nothing usable
    SM_ERR_TIMEOUT,          // processing exceeded the timeout (S2)
    SM_ERR_BACKEND,          // backend/network failure during a turn (S3)
    SM_ERR_CAPTURE,          // microphone / capture failure
    SM_ERR_PLAYBACK,         // reply audio stream failed
    SM_ERR_AI_UNAVAILABLE,   // backend reachable but AI provider not configured
    SM_ERR_COUNT
} sm_error_t;

typedef enum {
    SM_EV_BOOT_DONE = 0,
    SM_EV_NET_UP,
    SM_EV_NET_DOWN,
    SM_EV_BTN_PRESS,
    SM_EV_BTN_RELEASE,
    SM_EV_BTN_LONG,
    SM_EV_SPEECH_START,      // turn: VAD declared speech
    SM_EV_SILENCE_ENDPOINT,  // turn: silence after speech reached endpoint
    SM_EV_CAPTURE_LIMIT,     // turn: max recording length reached
    SM_EV_CAPTURE_FAILED,    // turn: microphone failure
    SM_EV_REPLY_READY,       // turn: validated reply metadata received
    SM_EV_REPLY_NOT_UNDERSTOOD, // turn
    SM_EV_TURN_FAILED,       // turn, code = sm_error_t
    SM_EV_PLAYBACK_DONE,     // turn, code = 0 ok / SM_ERR_PLAYBACK
    SM_EV_ERROR_CUE_DONE,
    SM_EV_TICK,
    SM_EV_COUNT
} sm_event_type_t;

typedef struct {
    sm_event_type_t type;
    uint32_t now_ms;
    uint32_t turn;
    int code;
} sm_event_t;

// Action bits
#define ACT_START_CAPTURE      (1u << 0)
#define ACT_DISCARD_CAPTURE    (1u << 1)
#define ACT_SUBMIT_CAPTURE     (1u << 2)
#define ACT_CANCEL_TURN        (1u << 3)   // abort network work for cancelled_turn
#define ACT_START_PLAYBACK     (1u << 4)
#define ACT_STOP_PLAYBACK      (1u << 5)
#define ACT_PLAY_LISTEN_CUE    (1u << 6)
#define ACT_PLAY_CANCEL_CUE    (1u << 7)
#define ACT_PLAY_ERROR         (1u << 8)   // error cue + spoken explanation for `error`
#define ACT_PLAY_OFFLINE       (1u << 9)   // spoken "I can't connect right now"
#define ACT_START_AFTERGLOW    (1u << 10)
#define ACT_RESET_SESSION      (1u << 11)  // conversation expired (10 min idle)

typedef enum {
    RSN_NONE = 0,
    RSN_NEW_TURN,
    RSN_FOLLOWUP,          // press within the continuation window
    RSN_INTERRUPT_PROCESSING,
    RSN_INTERRUPT_SPEAKING,
    RSN_CANCEL_LONG_PRESS,
    RSN_CANCEL_TAP,        // tap-to-finish with nothing said
    RSN_HANDS_FREE,
    RSN_SUBMIT_RELEASE,
    RSN_SUBMIT_ENDPOINT,
    RSN_SUBMIT_LIMIT,
    RSN_STALE_EVENT,       // event for a turn that is no longer active
    RSN_IGNORED,
} sm_reason_t;

typedef struct {
    uint32_t tap_ms;                // release earlier than this w/o speech => hands-free
    uint32_t silence_endpoint_ms;   // informational; endpoint detection is in audio task
    bool endpoint_while_held;       // honour silence endpoint while button held
    uint32_t no_speech_timeout_ms;  // hands-free listening with no speech
    uint32_t processing_timeout_ms; // protocol: 12 s
    uint32_t continuation_ms;       // protocol: 6 s
    uint32_t session_expiry_ms;     // protocol: 10 min
    uint32_t error_max_ms;          // fallback exit from ERROR
    uint32_t speaking_max_ms;       // fallback exit from SPEAKING
} sm_config_t;

typedef struct {
    sm_config_t cfg;
    dev_state_t state;
    uint32_t state_since;
    bool boot_done;
    bool net_ok;
    bool offline_notice_played;
    uint32_t turn;               // active turn id (0 = none)
    uint32_t next_turn;
    // listening sub-state
    bool held;
    bool hands_free;
    bool finishing;              // hands-free: press seen, waiting for release
    bool speech;
    uint32_t press_ms;
    uint32_t listen_start_ms;
    // conversation
    bool session_active;
    uint32_t last_activity_ms;
    uint32_t continuation_until_ms;
    sm_error_t last_error;
} sm_t;

typedef struct {
    uint32_t actions;
    dev_state_t from;
    dev_state_t to;
    bool changed;
    uint32_t new_turn;
    uint32_t cancelled_turn;
    sm_error_t error;
    sm_reason_t reason;
} sm_output_t;

void sm_default_config(sm_config_t *c);
void sm_init(sm_t *sm, const sm_config_t *cfg, uint32_t now_ms);
sm_output_t sm_handle(sm_t *sm, const sm_event_t *ev);

const char *sm_state_name(dev_state_t s);
const char *sm_error_name(sm_error_t e);
const char *sm_reason_name(sm_reason_t r);

#ifdef __cplusplus
}
#endif
