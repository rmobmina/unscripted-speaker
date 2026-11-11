// Host unit tests for the hardware-independent firmware core.
// Build & run:  make -C firmware/test
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "button_logic.h"
#include "byte_ring.h"
#include "led_engine.h"
#include "sm.h"
#include "vad.h"

static int g_checks, g_failures;
#define CHECK(cond)                                                                   \
    do {                                                                              \
        g_checks++;                                                                   \
        if (!(cond)) {                                                                \
            g_failures++;                                                             \
            fprintf(stderr, "  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);         \
        }                                                                             \
    } while (0)
#define RUN(fn)                                                                       \
    do {                                                                              \
        int before = g_failures;                                                      \
        fn();                                                                         \
        printf("%-48s %s\n", #fn, g_failures == before ? "ok" : "FAILED");            \
    } while (0)

// ---------------------------------------------------------------- button ---
static btn_event_t feed(btn_logic_t *b, bool level, uint32_t from, uint32_t to, btn_event_t *last_nonnone)
{
    btn_event_t got = BTN_EV_NONE;
    for (uint32_t t = from; t <= to; t += 5) {
        btn_event_t e = btn_update(b, level, t);
        if (e != BTN_EV_NONE) {
            got = e;
            if (last_nonnone) *last_nonnone = e;
        }
    }
    return got;
}

static void test_button_debounce_rejects_bounce(void)
{
    btn_logic_t b;
    btn_init(&b, 25, 2000);
    btn_update(&b, false, 0);
    int events = 0;
    // 3 ms bounces for 20 ms: never stable for 25 ms
    for (uint32_t t = 5; t < 25; t += 5) {
        if (btn_update(&b, (t / 5) % 2, t) != BTN_EV_NONE) events++;
    }
    CHECK(events == 0);
    // settle pressed
    btn_event_t e = BTN_EV_NONE;
    feed(&b, true, 25, 60, &e);
    CHECK(e == BTN_EV_PRESS);
}

static void test_button_press_release_long(void)
{
    btn_logic_t b;
    btn_init(&b, 25, 2000);
    btn_update(&b, false, 0);
    int presses = 0, releases = 0, longs = 0;
    for (uint32_t t = 5; t <= 3000; t += 5) {
        bool level = t >= 100 && t < 2600;
        btn_event_t e = btn_update(&b, level, t);
        if (e == BTN_EV_PRESS) presses++;
        if (e == BTN_EV_RELEASE) releases++;
        if (e == BTN_EV_LONG) {
            longs++;
            CHECK(t >= 2100 && t <= 2110);  // 2 s after the first edge (t=100)
        }
    }
    CHECK(presses == 1 && releases == 1 && longs == 1);
}

static void test_button_short_press_no_long(void)
{
    btn_logic_t b;
    btn_init(&b, 25, 2000);
    btn_update(&b, false, 0);
    int longs = 0;
    for (uint32_t t = 5; t <= 4000; t += 5) {
        if (btn_update(&b, t >= 100 && t < 1900, t) == BTN_EV_LONG) longs++;
    }
    CHECK(longs == 0);
}

static void test_button_held_at_boot_ignored(void)
{
    btn_logic_t b;
    btn_init(&b, 25, 2000);
    int ev = 0;
    for (uint32_t t = 0; t <= 3000; t += 5) {
        if (btn_update(&b, true, t) != BTN_EV_NONE) ev++;
    }
    CHECK(ev == 0);
}

// ----------------------------------------------------------- state machine ---
static sm_t sm;
static sm_output_t ev(sm_event_type_t type, uint32_t now, uint32_t turn, int code)
{
    sm_event_t e = {type, now, turn, code};
    return sm_handle(&sm, &e);
}

static void boot_online(uint32_t now)
{
    sm_config_t c;
    sm_default_config(&c);
    sm_init(&sm, &c, 0);
    ev(SM_EV_NET_UP, now, 0, 0);
    ev(SM_EV_BOOT_DONE, now, 0, 0);
}

static void test_sm_boot_to_idle_or_offline(void)
{
    boot_online(10);
    CHECK(sm.state == ST_IDLE);

    sm_config_t c;
    sm_default_config(&c);
    sm_init(&sm, &c, 0);
    sm_output_t o = ev(SM_EV_BOOT_DONE, 10, 0, 0);
    CHECK(sm.state == ST_OFFLINE);
    CHECK(o.actions & ACT_PLAY_OFFLINE);
    o = ev(SM_EV_NET_UP, 20, 0, 0);
    CHECK(sm.state == ST_IDLE);
}

static void test_sm_hold_to_talk_full_turn(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    CHECK(sm.state == ST_LISTENING);
    CHECK(o.actions & ACT_START_CAPTURE);
    CHECK(o.actions & ACT_PLAY_LISTEN_CUE);
    CHECK(!(o.actions & ACT_SUBMIT_CAPTURE));   // never upload on press-down
    uint32_t turn = o.new_turn;
    CHECK(turn != 0);
    ev(SM_EV_SPEECH_START, 1300, turn, 0);
    o = ev(SM_EV_BTN_RELEASE, 2500, 0, 0);
    CHECK(sm.state == ST_PROCESSING);
    CHECK(o.actions & ACT_SUBMIT_CAPTURE);
    o = ev(SM_EV_REPLY_READY, 4000, turn, 0);
    CHECK(sm.state == ST_SPEAKING);
    CHECK(o.actions & ACT_START_PLAYBACK);
    o = ev(SM_EV_PLAYBACK_DONE, 7000, turn, 0);
    CHECK(sm.state == ST_IDLE);
    CHECK(o.actions & ACT_START_AFTERGLOW);
}

static void test_sm_long_press_cancel_never_submits(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    uint32_t turn = o.new_turn;
    o = ev(SM_EV_BTN_LONG, 3000, 0, 0);
    CHECK(sm.state == ST_IDLE);
    CHECK(o.actions & ACT_DISCARD_CAPTURE);
    CHECK(o.actions & ACT_PLAY_CANCEL_CUE);
    CHECK(!(o.actions & ACT_SUBMIT_CAPTURE));
    CHECK(o.cancelled_turn == turn);
    CHECK(o.reason == RSN_CANCEL_LONG_PRESS);
    // the release that follows must not submit anything
    o = ev(SM_EV_BTN_RELEASE, 3200, 0, 0);
    CHECK(!(o.actions & ACT_SUBMIT_CAPTURE));
    CHECK(sm.state == ST_IDLE);
    // late capture events for the cancelled turn are stale
    o = ev(SM_EV_CAPTURE_LIMIT, 3300, turn, 0);
    CHECK(o.reason == RSN_STALE_EVENT && sm.state == ST_IDLE);
}

static void test_sm_long_press_while_talking_is_hold_to_talk(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    uint32_t turn = o.new_turn;
    ev(SM_EV_SPEECH_START, 1200, turn, 0);
    o = ev(SM_EV_BTN_LONG, 3000, 0, 0);
    CHECK(sm.state == ST_LISTENING);
    CHECK(o.reason == RSN_IGNORED);
    o = ev(SM_EV_BTN_RELEASE, 5000, 0, 0);
    CHECK(sm.state == ST_PROCESSING && (o.actions & ACT_SUBMIT_CAPTURE));
}

static void test_sm_tap_to_talk_endpoint(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    uint32_t turn = o.new_turn;
    o = ev(SM_EV_BTN_RELEASE, 1200, 0, 0);
    CHECK(sm.state == ST_LISTENING && sm.hands_free);
    CHECK(o.reason == RSN_HANDS_FREE);
    // silence endpoint before speech is ignored
    o = ev(SM_EV_SILENCE_ENDPOINT, 1500, turn, 0);
    CHECK(sm.state == ST_LISTENING);
    ev(SM_EV_SPEECH_START, 2000, turn, 0);
    o = ev(SM_EV_SILENCE_ENDPOINT, 5000, turn, 0);
    CHECK(sm.state == ST_PROCESSING && (o.actions & ACT_SUBMIT_CAPTURE));
    CHECK(o.reason == RSN_SUBMIT_ENDPOINT);
}

static void test_sm_hands_free_second_press(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    uint32_t turn = o.new_turn;
    ev(SM_EV_BTN_RELEASE, 1100, 0, 0);
    ev(SM_EV_SPEECH_START, 1500, turn, 0);
    o = ev(SM_EV_BTN_PRESS, 3000, 0, 0);       // finishing press: nothing sent yet
    CHECK(!(o.actions & ACT_SUBMIT_CAPTURE) && sm.state == ST_LISTENING);
    o = ev(SM_EV_BTN_RELEASE, 3100, 0, 0);
    CHECK(o.actions & ACT_SUBMIT_CAPTURE);
    // ... and a long press on the finishing press cancels instead
    boot_online(0);
    o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    turn = o.new_turn;
    ev(SM_EV_BTN_RELEASE, 1100, 0, 0);
    ev(SM_EV_SPEECH_START, 1500, turn, 0);
    ev(SM_EV_BTN_PRESS, 3000, 0, 0);
    o = ev(SM_EV_BTN_LONG, 5000, 0, 0);
    CHECK(sm.state == ST_IDLE && (o.actions & ACT_DISCARD_CAPTURE) && !(o.actions & ACT_SUBMIT_CAPTURE));
}

static void test_sm_hands_free_no_speech_timeout(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    ev(SM_EV_BTN_RELEASE, 1100, 0, 0);
    o = ev(SM_EV_TICK, 8900, 0, 0);
    CHECK(sm.state == ST_LISTENING);
    o = ev(SM_EV_TICK, 9001, 0, 0);
    CHECK(sm.state == ST_ERROR && o.error == SM_ERR_NO_SPEECH);
    CHECK(o.actions & ACT_DISCARD_CAPTURE);
    o = ev(SM_EV_ERROR_CUE_DONE, 11000, 0, 0);
    CHECK(sm.state == ST_IDLE);
}

static void test_sm_interrupt_speaking_starts_new_turn(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    uint32_t t1 = o.new_turn;
    ev(SM_EV_SPEECH_START, 1200, t1, 0);
    ev(SM_EV_BTN_RELEASE, 2000, 0, 0);
    ev(SM_EV_REPLY_READY, 3000, t1, 0);
    o = ev(SM_EV_BTN_PRESS, 3500, 0, 0);
    CHECK(sm.state == ST_LISTENING);
    CHECK(o.actions & ACT_STOP_PLAYBACK);
    CHECK(o.actions & ACT_CANCEL_TURN && o.cancelled_turn == t1);
    CHECK(o.new_turn != t1 && o.reason == RSN_INTERRUPT_SPEAKING);
    // playback-done from the old turn is rejected as stale
    o = ev(SM_EV_PLAYBACK_DONE, 3600, t1, 0);
    CHECK(o.reason == RSN_STALE_EVENT && sm.state == ST_LISTENING);
}

static void test_sm_interrupt_processing_rejects_stale_reply(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    uint32_t t1 = o.new_turn;
    ev(SM_EV_SPEECH_START, 1100, t1, 0);
    ev(SM_EV_BTN_RELEASE, 2000, 0, 0);
    o = ev(SM_EV_BTN_PRESS, 2500, 0, 0);
    uint32_t t2 = o.new_turn;
    CHECK(o.actions & ACT_CANCEL_TURN && o.cancelled_turn == t1);
    o = ev(SM_EV_REPLY_READY, 2600, t1, 0);        // old reply arrives late
    CHECK(o.reason == RSN_STALE_EVENT);
    CHECK(sm.state == ST_LISTENING && sm.turn == t2);
}

static void test_sm_processing_timeout(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    uint32_t t1 = o.new_turn;
    ev(SM_EV_SPEECH_START, 1100, t1, 0);
    ev(SM_EV_BTN_RELEASE, 2000, 0, 0);
    o = ev(SM_EV_TICK, 13999, 0, 0);
    CHECK(sm.state == ST_PROCESSING);
    o = ev(SM_EV_TICK, 14000, 0, 0);
    CHECK(sm.state == ST_ERROR && o.error == SM_ERR_TIMEOUT);
    CHECK(o.actions & ACT_CANCEL_TURN && o.cancelled_turn == t1);
    o = ev(SM_EV_REPLY_READY, 14500, t1, 0);
    CHECK(o.reason == RSN_STALE_EVENT && sm.state == ST_ERROR);
    o = ev(SM_EV_TICK, 24001, 0, 0);               // fallback exit from ERROR
    CHECK(sm.state == ST_IDLE);
}

static void test_sm_wifi_loss_and_recovery(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    o = ev(SM_EV_NET_DOWN, 1500, 0, 0);
    CHECK(sm.state == ST_OFFLINE);
    CHECK(o.actions & ACT_DISCARD_CAPTURE);
    CHECK(o.actions & ACT_PLAY_OFFLINE);
    o = ev(SM_EV_BTN_PRESS, 2000, 0, 0);
    CHECK(sm.state == ST_OFFLINE && (o.actions & ACT_PLAY_OFFLINE) && !(o.actions & ACT_START_CAPTURE));
    o = ev(SM_EV_NET_DOWN, 2500, 0, 0);             // repeated: no second notice
    CHECK(!(o.actions & ACT_PLAY_OFFLINE));
    ev(SM_EV_NET_UP, 5000, 0, 0);
    CHECK(sm.state == ST_IDLE);
    o = ev(SM_EV_NET_DOWN, 6000, 0, 0);             // new episode: notice again
    CHECK(o.actions & ACT_PLAY_OFFLINE);
}

static void test_sm_continuation_and_session_expiry(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    uint32_t t1 = o.new_turn;
    ev(SM_EV_SPEECH_START, 1100, t1, 0);
    ev(SM_EV_BTN_RELEASE, 2000, 0, 0);
    ev(SM_EV_REPLY_READY, 3000, t1, 0);
    ev(SM_EV_PLAYBACK_DONE, 5000, t1, 0);
    o = ev(SM_EV_BTN_PRESS, 10000, 0, 0);          // within 6 s
    CHECK(o.reason == RSN_FOLLOWUP);
    ev(SM_EV_BTN_LONG, 12000, 0, 0);               // cancel -> idle
    o = ev(SM_EV_BTN_PRESS, 20000, 0, 0);          // outside the window
    CHECK(o.reason == RSN_NEW_TURN);
    ev(SM_EV_BTN_LONG, 22000, 0, 0);
    o = ev(SM_EV_TICK, 22000 + 10 * 60 * 1000 - 1, 0, 0);
    CHECK(!(o.actions & ACT_RESET_SESSION));
    o = ev(SM_EV_TICK, 22000 + 10 * 60 * 1000, 0, 0);
    CHECK(o.actions & ACT_RESET_SESSION);
    o = ev(SM_EV_TICK, 22000 + 11 * 60 * 1000, 0, 0);
    CHECK(!(o.actions & ACT_RESET_SESSION));       // only once
}

static void test_sm_not_understood_and_failures(void)
{
    boot_online(0);
    sm_output_t o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    uint32_t t = o.new_turn;
    ev(SM_EV_BTN_RELEASE, 2000, 0, 0);             // held 1 s, no VAD speech: still submitted
    CHECK(sm.state == ST_PROCESSING);
    o = ev(SM_EV_REPLY_NOT_UNDERSTOOD, 3000, t, 0);
    CHECK(sm.state == ST_ERROR && o.error == SM_ERR_NOT_UNDERSTOOD && (o.actions & ACT_PLAY_ERROR));

    boot_online(0);
    o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    t = o.new_turn;
    o = ev(SM_EV_CAPTURE_FAILED, 1100, t, 0);
    CHECK(sm.state == ST_ERROR && o.error == SM_ERR_CAPTURE);

    boot_online(0);
    o = ev(SM_EV_BTN_PRESS, 1000, 0, 0);
    t = o.new_turn;
    ev(SM_EV_SPEECH_START, 1100, t, 0);
    ev(SM_EV_BTN_RELEASE, 2000, 0, 0);
    ev(SM_EV_REPLY_READY, 3000, t, 0);
    o = ev(SM_EV_PLAYBACK_DONE, 3500, t, SM_ERR_PLAYBACK);
    CHECK(sm.state == ST_ERROR && o.error == SM_ERR_PLAYBACK);
    o = ev(SM_EV_BTN_PRESS, 3600, 0, 0);           // press in ERROR starts a new turn
    CHECK(sm.state == ST_LISTENING && (o.actions & ACT_STOP_PLAYBACK));
}

static void test_sm_every_state_handles_every_event(void)
{
    // Fuzz: random event sequences must never produce an invalid state or
    // submit without being in LISTENING first.
    srand(1234);
    for (int run = 0; run < 2000; run++) {
        boot_online(0);
        uint32_t now = 0;
        for (int i = 0; i < 60; i++) {
            now += (uint32_t)(rand() % 3000);
            sm_event_type_t type = (sm_event_type_t)(rand() % SM_EV_COUNT);
            uint32_t turn = (rand() % 2) ? sm.turn : (uint32_t)(rand() % 5);
            dev_state_t before = sm.state;
            sm_output_t o = ev(type, now, turn, rand() % SM_ERR_COUNT);
            CHECK(sm.state >= ST_BOOTING && sm.state < ST_COUNT);
            if (o.actions & ACT_SUBMIT_CAPTURE) CHECK(before == ST_LISTENING && sm.state == ST_PROCESSING);
            if (o.actions & ACT_START_CAPTURE) CHECK(sm.state == ST_LISTENING && o.new_turn != 0);
            if (sm.state == ST_PROCESSING || sm.state == ST_SPEAKING || sm.state == ST_LISTENING)
                CHECK(sm.turn != 0);
        }
    }
}

// --------------------------------------------------------------- LED ring ---
static led_config_t cfg16(led_condition_t c)
{
    led_config_t k = {.count = 16, .offset = 0, .reverse = false, .condition = c,
                      .reduced_motion = false, .lights_off = false, .max_brightness_pct = 100,
                      .afterglow_ms = 10000};
    return k;
}

static void test_led_mapping_and_anchors(void)
{
    led_config_t k = cfg16(COND_B2);
    CHECK(led_logical_to_physical(&k, 0) == 0);
    k.offset = 5;
    CHECK(led_logical_to_physical(&k, 0) == 5);
    CHECK(led_logical_to_physical(&k, 11) == 0);
    k.reverse = true;
    CHECK(led_logical_to_physical(&k, 1) == 4);
    CHECK(led_logical_to_physical(&k, 6) == 15);
    int anchors = 0;
    for (int i = 0; i < 16; i++) anchors += led_is_anchor(i, 16);
    CHECK(anchors == 4);
    CHECK(led_is_anchor(0, 16) && led_is_anchor(4, 16) && led_is_anchor(8, 16) && led_is_anchor(12, 16));
    anchors = 0;
    for (int i = 0; i < 7; i++) anchors += led_is_anchor(i, 7);
    CHECK(anchors == 4);  // 7-pixel on-board ring: 4 anchors, 3 topic pixels
    // mapping is a permutation
    k = cfg16(COND_B2);
    k.offset = 9;
    k.reverse = true;
    int seen[16] = {0};
    for (int i = 0; i < 16; i++) seen[led_logical_to_physical(&k, i)]++;
    for (int i = 0; i < 16; i++) CHECK(seen[i] == 1);
}

static void test_led_status_levels_and_timing(void)
{
    // Listening: 4 s breathing between 25 % and 100 %
    float mn = 2, mx = -1;
    for (uint32_t t = 0; t < 4000; t += 20) {
        float l = led_status_level(LS_LISTENING, true, 0, 16, t);
        if (l < mn) mn = l;
        if (l > mx) mx = l;
    }
    CHECK(fabsf(mn - 0.25f) < 0.01f && fabsf(mx - 1.0f) < 0.01f);
    CHECK(fabsf(led_status_level(LS_LISTENING, true, 0, 16, 0) - led_status_level(LS_LISTENING, true, 0, 16, 4000)) < 1e-4f);
    // Speaking: 1.2 s pulse between 60 % and 100 %
    mn = 2; mx = -1;
    for (uint32_t t = 0; t < 1200; t += 20) {
        float l = led_status_level(LS_SPEAKING, true, 0, 16, t);
        if (l < mn) mn = l;
        if (l > mx) mx = l;
    }
    CHECK(fabsf(mn - 0.60f) < 0.01f && fabsf(mx - 1.0f) < 0.01f);
    // Processing: comet head at pixel 0 at t=0, pixel 4 at t=500 (1 lap / 2 s), clockwise
    CHECK(led_status_level(LS_PROCESSING, true, 0, 16, 0) > 0.99f);
    CHECK(led_status_level(LS_PROCESSING, true, 4, 16, 500) > 0.99f);
    CHECK(led_status_level(LS_PROCESSING, true, 0, 16, 2000) > 0.99f);
    CHECK(led_status_level(LS_PROCESSING, true, 8, 16, 0) < 0.1f);
    // Idle 10 %, offline: single pixel at 12 o'clock 8 %
    CHECK(fabsf(led_status_level(LS_IDLE, true, 3, 16, 5000) - 0.10f) < 1e-6f);
    CHECK(fabsf(led_status_level(LS_OFFLINE, true, 0, 16, 0) - 0.08f) < 1e-6f);
    CHECK(led_status_level(LS_OFFLINE, true, 1, 16, 0) == 0.f);
    // Error: two pulses over 1.2 s, then steady
    CHECK(led_status_level(LS_ERROR, true, 0, 16, 300) > 0.95f);
    CHECK(led_status_level(LS_ERROR, true, 0, 16, 900) > 0.95f);
    CHECK(led_status_level(LS_ERROR, true, 0, 16, 600) < 0.25f);
    CHECK(fabsf(led_status_level(LS_ERROR, true, 0, 16, 5000) - 0.20f) < 1e-6f);
    // B1 / reduced motion: every active state steady 70 %
    for (int s = LS_LISTENING; s <= LS_ERROR; s++)
        for (uint32_t t = 0; t < 5000; t += 137)
            CHECK(fabsf(led_status_level((led_status_t)s, false, 2, 16, t) - 0.70f) < 1e-6f);
}

static void test_led_no_rapid_flashing(void)
{
    // Frame-to-frame change must be gentle (no strobe): max per-frame delta of
    // any channel stays small for every animated state at 50 Hz.
    for (int s = LS_IDLE; s < LS_COUNT; s++) {
        float worst = 0;
        for (uint32_t t = 0; t < 8000; t += 20) {
            for (int i = 0; i < 16; i++) {
                float a = led_status_level((led_status_t)s, true, i, 16, t);
                float b = led_status_level((led_status_t)s, true, i, 16, t + 20);
                float d = fabsf(a - b);
                if (d > worst) worst = d;
            }
        }
        CHECK(worst < 0.25f);
    }
}

static void test_led_topic_layer_rules(void)
{
    led_engine_t e;
    rgb_t out[16];
    led_config_t k = cfg16(COND_B3);
    led_engine_init(&e, 0);
    led_engine_set_status(&e, LS_PROCESSING, 0);
    led_engine_set_topic(&e, TOPIC_FAMILY, 0.91f);
    led_engine_set_status(&e, LS_SPEAKING, 1000);
    led_engine_render(&e, &k, 1000 + 600, out);   // past cross-fade; pulse at min
    // anchors keep green status, others show family pink (scaled by pulse level)
    CHECK(out[0].g > out[0].r && out[4].g > out[4].r);
    CHECK(out[1].r > out[1].g && out[2].r > out[2].b);

    // B2: no topic even when a topic is set
    k.condition = COND_B2;
    led_engine_render(&e, &k, 1000 + 600, out);
    CHECK(out[1].g > out[1].r);

    // low confidence -> general (neutral warm white)
    CHECK(led_topic_sanitize(TOPIC_FAMILY, 0.59f) == TOPIC_GENERAL);
    CHECK(led_topic_sanitize(TOPIC_FAMILY, 0.60f) == TOPIC_FAMILY);
    CHECK(led_topic_sanitize(99, 0.99f) == TOPIC_GENERAL);
    CHECK(led_topic_sanitize(TOPIC_ROUTINE, NAN) == TOPIC_GENERAL);
    CHECK(led_topic_sanitize(TOPIC_ROUTINE, 1.5f) == TOPIC_GENERAL);

    // topic never overrides LISTENING: new interaction clears the topic
    k.condition = COND_B3;
    led_engine_set_status(&e, LS_LISTENING, 2000);
    led_engine_render(&e, &k, 2200, out);
    for (int i = 0; i < 16; i++) CHECK(out[i].b > out[i].r);
    CHECK(!e.topic_valid);
}

static void test_led_afterglow(void)
{
    led_engine_t e;
    rgb_t out[16];
    led_config_t k = cfg16(COND_B3);
    led_engine_init(&e, 0);
    led_engine_set_topic(&e, TOPIC_RELAXATION, 0.8f);
    led_engine_set_status(&e, LS_SPEAKING, 0);
    led_engine_set_status(&e, LS_IDLE, 5000);
    led_engine_start_afterglow(&e, &k, 5000);
    led_engine_render(&e, &k, 5200, out);
    CHECK(out[1].b > out[1].g);        // lavender-ish on a non-anchor
    CHECK(out[0].r >= out[0].b);       // anchor: warm idle
    led_engine_render(&e, &k, 15001, out);
    rgb_t idle = {(uint8_t)lrintf(255 * 0.1f), (uint8_t)lrintf(217 * 0.1f), (uint8_t)lrintf(168 * 0.1f)};
    CHECK(out[1].r == idle.r && out[1].g == idle.g && out[1].b == idle.b);
}

static void test_led_accessibility_and_cap(void)
{
    led_engine_t e;
    rgb_t out[16];
    led_config_t k = cfg16(COND_B2);
    led_engine_init(&e, 0);
    led_engine_set_status(&e, LS_LISTENING, 0);
    k.max_brightness_pct = 50;
    led_engine_render(&e, &k, 1000 * 4, out);   // peak of breathing
    CHECK(out[0].b <= 128);
    k.lights_off = true;
    led_engine_render(&e, &k, 1000, out);
    for (int i = 0; i < 16; i++) CHECK(out[i].r == 0 && out[i].g == 0 && out[i].b == 0);
    k.lights_off = false;
    k.reduced_motion = true;
    k.max_brightness_pct = 100;
    led_engine_render(&e, &k, 1000, out);
    rgb_t a = out[3];
    led_engine_render(&e, &k, 2337, out);
    CHECK(a.r == out[3].r && a.g == out[3].g && a.b == out[3].b);  // steady
}

static void test_led_transition_is_eased(void)
{
    led_engine_t e;
    rgb_t out[16];
    led_config_t k = cfg16(COND_B2);
    led_engine_init(&e, 0);
    led_engine_set_status(&e, LS_IDLE, 0);
    led_engine_render(&e, &k, 500, out);
    led_engine_set_status(&e, LS_LISTENING, 1000);
    led_engine_render(&e, &k, 1060, out);       // mid fade
    CHECK(out[0].b > 17 && out[0].b < 255);
    led_engine_render(&e, &k, 1000 + LED_TRANSITION_MS + 1, out);
    CHECK(!e.transitioning);
    // listening indication reaches >= 90 % blue within 150 ms of the press
    led_engine_set_status(&e, LS_IDLE, 2000);
    led_engine_render(&e, &k, 2500, out);
    led_engine_set_status(&e, LS_LISTENING, 3000);
    led_engine_render(&e, &k, 3150, out);
    CHECK(out[0].b >= 230);
}

// -------------------------------------------------------------------- VAD ---
static void tone(int16_t *buf, size_t n, float amp, float hz, uint32_t *phase)
{
    for (size_t i = 0; i < n; i++, (*phase)++) buf[i] = (int16_t)(amp * 32767.f * sinf(2.f * 3.14159265f * hz * (*phase) / 16000.f));
}

static void test_vad_detects_speech_and_silence(void)
{
    vad_config_t c;
    vad_default_config(&c);
    vad_t v;
    vad_reset(&v, &c);
    int16_t f[320];
    uint32_t ph = 0;
    srand(7);
    for (int i = 0; i < 25; i++) {               // 500 ms of low noise
        for (int j = 0; j < 320; j++) f[j] = (int16_t)((rand() % 61) - 30);
        CHECK(!vad_process(&v, f, 320, NULL));
    }
    CHECK(!v.speech_detected);
    int started_at = -1;
    for (int i = 0; i < 25; i++) {               // 500 ms of "speech" at -12 dBFS
        tone(f, 320, 0.25f, 220.f, &ph);
        if (vad_process(&v, f, 320, NULL)) started_at = i;
    }
    CHECK(started_at == 4);                       // 100 ms start debounce
    CHECK(v.speech_detected);
    for (int i = 0; i < 75; i++) {               // 1.5 s silence
        for (int j = 0; j < 320; j++) f[j] = (int16_t)((rand() % 61) - 30);
        vad_process(&v, f, 320, NULL);
    }
    CHECK(v.silence_run_ms >= 1500);
    CHECK(vad_quality(&v) == CAPTURE_OK);
}

static void test_vad_quality_flags(void)
{
    vad_config_t c;
    vad_default_config(&c);
    vad_t v;
    int16_t f[320];
    vad_reset(&v, &c);
    memset(f, 0, sizeof(f));
    for (int i = 0; i < 10; i++) vad_process(&v, f, 320, NULL);
    CHECK(vad_quality(&v) == CAPTURE_NO_SIGNAL);

    vad_reset(&v, &c);
    for (int j = 0; j < 320; j++) f[j] = (int16_t)((j % 7) - 3);
    for (int i = 0; i < 10; i++) vad_process(&v, f, 320, NULL);
    CHECK(vad_quality(&v) == CAPTURE_SILENT);

    vad_reset(&v, &c);
    for (int j = 0; j < 320; j++) f[j] = (j % 2) ? 32767 : -32768;
    for (int i = 0; i < 10; i++) vad_process(&v, f, 320, NULL);
    CHECK(vad_quality(&v) == CAPTURE_CLIPPED);
}

// ------------------------------------------------------------------- ring ---
static void test_ring_bounds_and_wrap(void)
{
    uint8_t store[10];
    byte_ring_t r;
    ring_init(&r, store, sizeof(store));
    uint8_t in[16], outb[16];
    for (int i = 0; i < 16; i++) in[i] = (uint8_t)i;
    CHECK(ring_write(&r, in, 16) == 10);         // never exceeds capacity
    CHECK(ring_free(&r) == 0);
    CHECK(ring_read(&r, outb, 4) == 4 && outb[0] == 0 && outb[3] == 3);
    CHECK(ring_write(&r, in + 10, 4) == 4);      // wraps
    CHECK(ring_read(&r, outb, 16) == 10);
    CHECK(outb[0] == 4 && outb[5] == 9 && outb[6] == 10 && outb[9] == 13);
    CHECK(ring_used(&r) == 0);
    ring_write(&r, in, 5);
    ring_reset(&r);
    CHECK(ring_used(&r) == 0 && ring_read(&r, outb, 4) == 0);
}

int main(void)
{
    setvbuf(stdout, NULL, _IONBF, 0);
    RUN(test_button_debounce_rejects_bounce);
    RUN(test_button_press_release_long);
    RUN(test_button_short_press_no_long);
    RUN(test_button_held_at_boot_ignored);
    RUN(test_sm_boot_to_idle_or_offline);
    RUN(test_sm_hold_to_talk_full_turn);
    RUN(test_sm_long_press_cancel_never_submits);
    RUN(test_sm_long_press_while_talking_is_hold_to_talk);
    RUN(test_sm_tap_to_talk_endpoint);
    RUN(test_sm_hands_free_second_press);
    RUN(test_sm_hands_free_no_speech_timeout);
    RUN(test_sm_interrupt_speaking_starts_new_turn);
    RUN(test_sm_interrupt_processing_rejects_stale_reply);
    RUN(test_sm_processing_timeout);
    RUN(test_sm_wifi_loss_and_recovery);
    RUN(test_sm_continuation_and_session_expiry);
    RUN(test_sm_not_understood_and_failures);
    RUN(test_sm_every_state_handles_every_event);
    RUN(test_led_mapping_and_anchors);
    RUN(test_led_status_levels_and_timing);
    RUN(test_led_no_rapid_flashing);
    RUN(test_led_topic_layer_rules);
    RUN(test_led_afterglow);
    RUN(test_led_accessibility_and_cap);
    RUN(test_led_transition_is_eased);
    RUN(test_vad_detects_speech_and_silence);
    RUN(test_vad_quality_flags);
    RUN(test_ring_bounds_and_wrap);
    printf("\n%d checks, %d failures\n", g_checks, g_failures);
    return g_failures ? 1 : 0;
}
