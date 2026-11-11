#include "sm.h"

#include <string.h>

void sm_default_config(sm_config_t *c)
{
    c->tap_ms = 700;
    c->silence_endpoint_ms = 1500;
    c->endpoint_while_held = false;
    c->no_speech_timeout_ms = 8000;
    c->processing_timeout_ms = 12000;
    c->continuation_ms = 6000;
    c->session_expiry_ms = 10u * 60u * 1000u;
    c->error_max_ms = 10000;
    c->speaking_max_ms = 120000;
}

void sm_init(sm_t *sm, const sm_config_t *cfg, uint32_t now)
{
    memset(sm, 0, sizeof(*sm));
    sm->cfg = *cfg;
    sm->state = ST_BOOTING;
    sm->state_since = now;
    sm->next_turn = 1;
}

static bool elapsed(uint32_t now, uint32_t since, uint32_t ms) { return (uint32_t)(now - since) >= ms; }

static void go(sm_t *sm, sm_output_t *o, dev_state_t s, uint32_t now)
{
    if (sm->state != s) {
        sm->state = s;
        sm->state_since = now;
        o->changed = true;
    }
    o->to = s;
}

static void begin_listening(sm_t *sm, sm_output_t *o, uint32_t now, sm_reason_t why)
{
    sm->turn = sm->next_turn++;
    if (sm->next_turn == 0) sm->next_turn = 1;
    sm->held = true;
    sm->hands_free = false;
    sm->finishing = false;
    sm->speech = false;
    sm->press_ms = now;
    sm->listen_start_ms = now;
    sm->session_active = true;
    sm->last_activity_ms = now;
    o->new_turn = sm->turn;
    o->actions |= ACT_START_CAPTURE | ACT_PLAY_LISTEN_CUE;
    o->reason = why;
    go(sm, o, ST_LISTENING, now);
}

static void submit(sm_t *sm, sm_output_t *o, uint32_t now, sm_reason_t why)
{
    o->actions |= ACT_SUBMIT_CAPTURE;
    o->reason = why;
    sm->held = sm->hands_free = sm->finishing = false;
    sm->last_activity_ms = now;
    go(sm, o, ST_PROCESSING, now);
}

static void cancel_to_idle(sm_t *sm, sm_output_t *o, uint32_t now, sm_reason_t why)
{
    o->actions |= ACT_DISCARD_CAPTURE | ACT_PLAY_CANCEL_CUE;
    o->cancelled_turn = sm->turn;
    o->reason = why;
    sm->turn = 0;
    sm->held = sm->hands_free = sm->finishing = false;
    sm->last_activity_ms = now;
    go(sm, o, ST_IDLE, now);
}

static void to_error(sm_t *sm, sm_output_t *o, uint32_t now, sm_error_t err)
{
    o->actions |= ACT_PLAY_ERROR;
    o->error = err;
    sm->last_error = err;
    sm->turn = 0;
    sm->held = sm->hands_free = sm->finishing = false;
    go(sm, o, ST_ERROR, now);
}

static void to_offline(sm_t *sm, sm_output_t *o, uint32_t now)
{
    switch (sm->state) {
    case ST_LISTENING:
        o->actions |= ACT_DISCARD_CAPTURE;
        o->cancelled_turn = sm->turn;
        break;
    case ST_PROCESSING:
        o->actions |= ACT_CANCEL_TURN;
        o->cancelled_turn = sm->turn;
        break;
    case ST_SPEAKING:
    case ST_ERROR:
        o->actions |= ACT_STOP_PLAYBACK | ACT_CANCEL_TURN;
        o->cancelled_turn = sm->turn;
        break;
    default:
        break;
    }
    sm->turn = 0;
    sm->held = sm->hands_free = sm->finishing = false;
    if (!sm->offline_notice_played) {
        o->actions |= ACT_PLAY_OFFLINE;
        sm->offline_notice_played = true;
    }
    go(sm, o, ST_OFFLINE, now);
}

static bool turn_event(sm_event_type_t t)
{
    return t == SM_EV_SPEECH_START || t == SM_EV_SILENCE_ENDPOINT || t == SM_EV_CAPTURE_LIMIT ||
           t == SM_EV_CAPTURE_FAILED || t == SM_EV_REPLY_READY || t == SM_EV_REPLY_NOT_UNDERSTOOD ||
           t == SM_EV_TURN_FAILED || t == SM_EV_PLAYBACK_DONE;
}

static void on_press(sm_t *sm, sm_output_t *o, uint32_t now)
{
    switch (sm->state) {
    case ST_IDLE: {
        bool followup = sm->continuation_until_ms != 0 && (int32_t)(sm->continuation_until_ms - now) > 0;
        sm->continuation_until_ms = 0;
        begin_listening(sm, o, now, followup ? RSN_FOLLOWUP : RSN_NEW_TURN);
        break;
    }
    case ST_PROCESSING:
        o->actions |= ACT_CANCEL_TURN;
        o->cancelled_turn = sm->turn;
        begin_listening(sm, o, now, RSN_INTERRUPT_PROCESSING);
        break;
    case ST_SPEAKING:
        o->actions |= ACT_STOP_PLAYBACK | ACT_CANCEL_TURN;
        o->cancelled_turn = sm->turn;
        begin_listening(sm, o, now, RSN_INTERRUPT_SPEAKING);
        break;
    case ST_ERROR:
        o->actions |= ACT_STOP_PLAYBACK;
        begin_listening(sm, o, now, RSN_NEW_TURN);
        break;
    case ST_LISTENING:
        if (sm->hands_free && !sm->finishing) {
            sm->finishing = true;      // decide on release (or cancel on long press)
            sm->press_ms = now;
            o->reason = RSN_NONE;
        } else {
            o->reason = RSN_IGNORED;
        }
        break;
    case ST_OFFLINE:
        o->actions |= ACT_PLAY_OFFLINE;  // honest: "I can't connect right now"
        o->reason = RSN_IGNORED;
        break;
    default:
        o->reason = RSN_IGNORED;
        break;
    }
}

static void on_release(sm_t *sm, sm_output_t *o, uint32_t now)
{
    if (sm->state != ST_LISTENING) return;
    if (sm->finishing) {
        if (sm->speech) {
            submit(sm, o, now, RSN_SUBMIT_RELEASE);
        } else {
            cancel_to_idle(sm, o, now, RSN_CANCEL_TAP);
        }
        return;
    }
    if (!sm->held) return;
    sm->held = false;
    uint32_t held = now - sm->press_ms;
    if (sm->speech) {
        submit(sm, o, now, RSN_SUBMIT_RELEASE);
    } else if (held < sm->cfg.tap_ms) {
        sm->hands_free = true;
        o->reason = RSN_HANDS_FREE;
    } else {
        // Held for a while but the VAD heard nothing: let speech-to-text decide
        // (quiet speakers must not be silently ignored).
        submit(sm, o, now, RSN_SUBMIT_RELEASE);
    }
}

static void on_long(sm_t *sm, sm_output_t *o, uint32_t now)
{
    if (sm->state != ST_LISTENING) return;
    if (sm->finishing || (sm->held && !sm->speech)) {
        cancel_to_idle(sm, o, now, RSN_CANCEL_LONG_PRESS);
    } else {
        o->reason = RSN_IGNORED;  // talking while holding = hold-to-talk
    }
}

static void on_tick(sm_t *sm, sm_output_t *o, uint32_t now)
{
    switch (sm->state) {
    case ST_LISTENING:
        if (sm->hands_free && !sm->finishing && !sm->speech &&
            elapsed(now, sm->listen_start_ms, sm->cfg.no_speech_timeout_ms)) {
            o->actions |= ACT_DISCARD_CAPTURE;
            o->cancelled_turn = sm->turn;
            to_error(sm, o, now, SM_ERR_NO_SPEECH);
        }
        break;
    case ST_PROCESSING:
        if (elapsed(now, sm->state_since, sm->cfg.processing_timeout_ms)) {
            o->actions |= ACT_CANCEL_TURN;
            o->cancelled_turn = sm->turn;
            to_error(sm, o, now, SM_ERR_TIMEOUT);
        }
        break;
    case ST_SPEAKING:
        if (elapsed(now, sm->state_since, sm->cfg.speaking_max_ms)) {
            o->actions |= ACT_STOP_PLAYBACK | ACT_CANCEL_TURN;
            o->cancelled_turn = sm->turn;
            sm->turn = 0;
            go(sm, o, ST_IDLE, now);
        }
        break;
    case ST_ERROR:
        if (elapsed(now, sm->state_since, sm->cfg.error_max_ms)) {
            o->actions |= ACT_STOP_PLAYBACK;
            go(sm, o, sm->net_ok ? ST_IDLE : ST_OFFLINE, now);
        }
        break;
    default:
        break;
    }
    if (sm->session_active && (sm->state == ST_IDLE || sm->state == ST_OFFLINE) &&
        elapsed(now, sm->last_activity_ms, sm->cfg.session_expiry_ms)) {
        sm->session_active = false;
        o->actions |= ACT_RESET_SESSION;
    }
}

sm_output_t sm_handle(sm_t *sm, const sm_event_t *ev)
{
    sm_output_t o = {0};
    uint32_t now = ev->now_ms;
    o.from = o.to = sm->state;

    if (turn_event(ev->type) && (ev->turn == 0 || ev->turn != sm->turn)) {
        o.reason = RSN_STALE_EVENT;
        return o;
    }

    switch (ev->type) {
    case SM_EV_BOOT_DONE:
        sm->boot_done = true;
        if (sm->state == ST_BOOTING) {
            if (sm->net_ok) go(sm, &o, ST_IDLE, now);
            else to_offline(sm, &o, now);
        }
        break;
    case SM_EV_NET_UP:
        sm->net_ok = true;
        sm->offline_notice_played = false;
        if (sm->state == ST_OFFLINE || (sm->state == ST_BOOTING && sm->boot_done)) go(sm, &o, ST_IDLE, now);
        break;
    case SM_EV_NET_DOWN:
        sm->net_ok = false;
        if (sm->state != ST_BOOTING && sm->state != ST_OFFLINE) to_offline(sm, &o, now);
        break;
    case SM_EV_BTN_PRESS:
        on_press(sm, &o, now);
        break;
    case SM_EV_BTN_RELEASE:
        on_release(sm, &o, now);
        break;
    case SM_EV_BTN_LONG:
        on_long(sm, &o, now);
        break;
    case SM_EV_SPEECH_START:
        if (sm->state == ST_LISTENING) sm->speech = true;
        break;
    case SM_EV_SILENCE_ENDPOINT:
        if (sm->state == ST_LISTENING && sm->speech && !sm->finishing &&
            (sm->hands_free || sm->cfg.endpoint_while_held)) {
            submit(sm, &o, now, RSN_SUBMIT_ENDPOINT);
        }
        break;
    case SM_EV_CAPTURE_LIMIT:
        if (sm->state == ST_LISTENING) submit(sm, &o, now, RSN_SUBMIT_LIMIT);
        break;
    case SM_EV_CAPTURE_FAILED:
        if (sm->state == ST_LISTENING) {
            o.actions |= ACT_DISCARD_CAPTURE;
            to_error(sm, &o, now, SM_ERR_CAPTURE);
        }
        break;
    case SM_EV_REPLY_READY:
        if (sm->state == ST_PROCESSING) {
            o.actions |= ACT_START_PLAYBACK;
            sm->last_activity_ms = now;
            go(sm, &o, ST_SPEAKING, now);
        }
        break;
    case SM_EV_REPLY_NOT_UNDERSTOOD:
        if (sm->state == ST_PROCESSING) to_error(sm, &o, now, SM_ERR_NOT_UNDERSTOOD);
        break;
    case SM_EV_TURN_FAILED:
        if (sm->state == ST_PROCESSING || sm->state == ST_SPEAKING) {
            if (sm->state == ST_SPEAKING) o.actions |= ACT_STOP_PLAYBACK;
            sm_error_t e = (ev->code > SM_ERR_NONE && ev->code < SM_ERR_COUNT) ? (sm_error_t)ev->code : SM_ERR_BACKEND;
            to_error(sm, &o, now, e);
        }
        break;
    case SM_EV_PLAYBACK_DONE:
        if (sm->state == ST_SPEAKING) {
            sm->last_activity_ms = now;
            if (ev->code != 0) {
                to_error(sm, &o, now, SM_ERR_PLAYBACK);
            } else {
                sm->turn = 0;
                sm->continuation_until_ms = now + sm->cfg.continuation_ms;
                if (sm->continuation_until_ms == 0) sm->continuation_until_ms = 1;
                o.actions |= ACT_START_AFTERGLOW;
                go(sm, &o, ST_IDLE, now);
            }
        }
        break;
    case SM_EV_ERROR_CUE_DONE:
        if (sm->state == ST_ERROR) go(sm, &o, sm->net_ok ? ST_IDLE : ST_OFFLINE, now);
        break;
    case SM_EV_TICK:
        on_tick(sm, &o, now);
        break;
    default:
        break;
    }
    return o;
}

const char *sm_state_name(dev_state_t s)
{
    static const char *n[ST_COUNT] = {"BOOTING", "IDLE", "LISTENING", "PROCESSING", "SPEAKING", "ERROR", "OFFLINE"};
    return (s >= 0 && s < ST_COUNT) ? n[s] : "?";
}

const char *sm_error_name(sm_error_t e)
{
    static const char *n[SM_ERR_COUNT] = {"none", "no_speech", "not_understood", "processing_timeout",
                                       "backend_error", "capture_error", "playback_error", "ai_unavailable"};
    return (e >= 0 && e < SM_ERR_COUNT) ? n[e] : "?";
}

const char *sm_reason_name(sm_reason_t r)
{
    switch (r) {
    case RSN_NONE: return "";
    case RSN_NEW_TURN: return "new_turn";
    case RSN_FOLLOWUP: return "followup";
    case RSN_INTERRUPT_PROCESSING: return "interrupt_processing";
    case RSN_INTERRUPT_SPEAKING: return "interrupt_speaking";
    case RSN_CANCEL_LONG_PRESS: return "long_press";
    case RSN_CANCEL_TAP: return "tap_without_speech";
    case RSN_HANDS_FREE: return "hands_free";
    case RSN_SUBMIT_RELEASE: return "release";
    case RSN_SUBMIT_ENDPOINT: return "silence_endpoint";
    case RSN_SUBMIT_LIMIT: return "max_length";
    case RSN_STALE_EVENT: return "stale_event";
    case RSN_IGNORED: return "ignored";
    }
    return "?";
}
