// [un]scripted 2.0 device firmware - controller.
//
// The controller task is the only owner of the interaction state machine
// (core/sm.c). Button, audio, network and timer events arrive through one
// bounded queue; the resulting actions are dispatched to the audio task, LED
// task and backend workers, none of which block the controller.
#include <stdio.h>
#include <string.h>

#include "app_config.h"
#include "app_shared.h"
#include "esp_chip_info.h"
#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_psram.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "hw/audio.h"
#include "hw/board.h"
#include "hw/button.h"
#include "hw/leds.h"
#include "net/backend.h"
#include "net/telemetry.h"
#include "net/wifi.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "pins.h"
#include "secrets.h"

static const char *TAG = "ctrl";

static QueueHandle_t s_q;
static sm_t s_sm;
static volatile uint32_t s_active_turn;
static volatile dev_state_t s_state = ST_BOOTING;
static char s_boot_id[12];
static char s_session[48];
static dev_settings_t s_settings;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }
uint32_t active_turn(void) { return s_active_turn; }
dev_state_t device_state(void) { return s_state; }
const char *device_boot_id(void) { return s_boot_id; }

void turn_id_string(uint32_t turn, char *out, int cap) { snprintf(out, cap, "%s-%lu", s_boot_id, (unsigned long)turn); }

void session_get(char *out, int cap)
{
    taskENTER_CRITICAL(&s_lock);
    strlcpy(out, s_session, cap);
    taskEXIT_CRITICAL(&s_lock);
}

void session_set(const char *sid)
{
    taskENTER_CRITICAL(&s_lock);
    strlcpy(s_session, sid ? sid : "", sizeof(s_session));
    taskEXIT_CRITICAL(&s_lock);
}

void session_clear(void) { session_set(""); }

// ------------------------------------------------------------- settings ----
void settings_defaults(dev_settings_t *s)
{
    *s = (dev_settings_t){
        .version = 0,
        .condition = COND_B2,
        .reduced_motion = false,
        .lights_off = false,
        .max_brightness_pct = LED_DEFAULT_BRIGHTNESS_PCT,
        .volume = AUDIO_DEFAULT_VOLUME,
        .led_offset = LED_INDEX_OFFSET,
        .led_reverse = LED_INDEX_REVERSE,
        .led_grb = false,
        .afterglow_ms = LED_AFTERGLOW_MS,
        .silence_endpoint_ms = SILENCE_ENDPOINT_MS,
        .endpoint_while_held = false,
        .vad_abs_dbfs = -48.0f,
        .vad_snr_db = 9.0f,
        .session_reset_seq = 0,
    };
}

void settings_get(dev_settings_t *out)
{
    taskENTER_CRITICAL(&s_lock);
    *out = s_settings;
    taskEXIT_CRITICAL(&s_lock);
}

void settings_apply(const dev_settings_t *in)
{
    bool changed;
    taskENTER_CRITICAL(&s_lock);
    changed = memcmp(&s_settings, in, sizeof(*in)) != 0;
    s_settings = *in;
    taskEXIT_CRITICAL(&s_lock);
    if (!changed) return;
    nvs_handle_t h;
    if (nvs_open("uns", NVS_READWRITE, &h) == ESP_OK) {   // condition survives reboot
        nvs_set_blob(h, "settings", in, sizeof(*in));
        nvs_commit(h);
        nvs_close(h);
    }
}

void settings_load(void)
{
    dev_settings_t s;
    settings_defaults(&s);
    nvs_handle_t h;
    if (nvs_open("uns", NVS_READONLY, &h) == ESP_OK) {
        dev_settings_t stored;
        size_t len = sizeof(stored);
        if (nvs_get_blob(h, "settings", &stored, &len) == ESP_OK && len == sizeof(stored)) s = stored;
        nvs_close(h);
    }
    taskENTER_CRITICAL(&s_lock);
    s_settings = s;
    taskEXIT_CRITICAL(&s_lock);
}

// ---------------------------------------------------------------- mailbox ---
void ctrl_post(const ctrl_msg_t *m)
{
    if (!s_q) return;
    if (xQueueSend(s_q, m, 0) != pdTRUE) ESP_LOGW(TAG, "controller queue full; dropped msg %d", m->type);
}

void ctrl_post_event(sm_event_type_t type, uint32_t turn, int code)
{
    ctrl_msg_t m = {.type = MSG_SM, .ev = {.type = type, .now_ms = now_ms(), .turn = turn, .code = code}};
    ctrl_post(&m);
}

// ------------------------------------------------------------- dispatch -----
static void apply_sm_config(void)
{
    dev_settings_t st;
    settings_get(&st);
    s_sm.cfg.silence_endpoint_ms = st.silence_endpoint_ms;
    s_sm.cfg.endpoint_while_held = st.endpoint_while_held;
}

static void handle_sm_event(const sm_event_t *ev)
{
    switch (ev->type) {
    case SM_EV_BTN_PRESS: telemetry_log(TEL_BUTTON_PRESSED, s_sm.turn, 0, 0, sm_state_name(s_sm.state)); break;
    case SM_EV_BTN_RELEASE: telemetry_log(TEL_BUTTON_RELEASED, s_sm.turn, 0, 0, NULL); break;
    case SM_EV_BTN_LONG: telemetry_log(TEL_BUTTON_LONG_PRESS, s_sm.turn, 0, 0, NULL); break;
    default: break;
    }

    sm_output_t o = sm_handle(&s_sm, ev);
    s_active_turn = s_sm.turn;      // workers for older turns stop immediately
    s_state = s_sm.state;

    if (o.reason == RSN_STALE_EVENT && ev->type != SM_EV_TICK) {
        telemetry_log(TEL_STALE_RESPONSE_REJECTED, ev->turn, ev->type, 0, "stale_event");
        return;
    }
    if (o.changed) {
        leds_set_status((led_status_t)o.to);   // LED first: <=150 ms listening indication
        char d[28];
        snprintf(d, sizeof(d), "%s>%s", sm_state_name(o.from), sm_state_name(o.to));
        telemetry_log(TEL_STATE_CHANGED, s_sm.turn, 0, 0, d);
        ESP_LOGI(TAG, "state %s (%s)", d, sm_reason_name(o.reason));
    }
    uint32_t a = o.actions;
    if (a & ACT_STOP_PLAYBACK) audio_stop_playback();
    if (a & ACT_CANCEL_TURN) {
        backend_cancel_turn(o.cancelled_turn);
        telemetry_log(TEL_INTERACTION_CANCELLED, o.cancelled_turn, 0, 0, sm_reason_name(o.reason));
    }
    if (a & ACT_DISCARD_CAPTURE) {
        audio_capture_discard();
        telemetry_log(TEL_LISTENING_ENDED, o.cancelled_turn, 0, 0, "discarded");
        if (o.reason == RSN_CANCEL_LONG_PRESS || o.reason == RSN_CANCEL_TAP)
            telemetry_log(TEL_INTERACTION_CANCELLED, o.cancelled_turn, 0, 0, sm_reason_name(o.reason));
    }
    if (a & ACT_START_CAPTURE) {
        backend_mark_followup(o.new_turn, o.reason == RSN_FOLLOWUP);
        audio_capture_start(o.new_turn);    // includes the soft listening chime
        telemetry_log(TEL_LISTENING_STARTED, o.new_turn, 0, 0, sm_reason_name(o.reason));
    }
    if (a & ACT_SUBMIT_CAPTURE) {
        audio_capture_submit(s_sm.turn);
        telemetry_log(TEL_LISTENING_ENDED, s_sm.turn, 0, 0, sm_reason_name(o.reason));
    }
    if (a & ACT_START_PLAYBACK) audio_stream_play(s_sm.turn);
    if (a & ACT_PLAY_CANCEL_CUE) audio_play_cue(CUE_CANCEL);
    if (a & ACT_PLAY_ERROR) {
        if (o.error == SM_ERR_TIMEOUT) telemetry_log(TEL_PROCESSING_TIMEOUT, o.cancelled_turn, PROCESSING_TIMEOUT_MS, 0, NULL);
        telemetry_log(TEL_ERROR, o.cancelled_turn ? o.cancelled_turn : ev->turn, 0, o.error, sm_error_name(o.error));
        audio_play_error(o.error);
    }
    if (a & ACT_PLAY_OFFLINE) audio_play_cue(CUE_OFFLINE);
    if (a & ACT_START_AFTERGLOW) leds_start_afterglow();
    if (a & ACT_RESET_SESSION) {
        session_clear();
        telemetry_log(TEL_SESSION_RESET, 0, 0, 0, "inactivity");
    }
}

static void controller_task(void *arg)
{
    uint32_t last_tick = now_ms();
    dev_settings_t prev;
    settings_get(&prev);
    for (;;) {
        ctrl_msg_t m;
        if (xQueueReceive(s_q, &m, pdMS_TO_TICKS(20)) == pdTRUE) {
            switch (m.type) {
            case MSG_SM:
                handle_sm_event(&m.ev);
                break;
            case MSG_REPLY_META:
                if (m.ev.turn == s_sm.turn) leds_set_topic(m.topic, m.topic_confidence);
                break;
            case MSG_SETTINGS: {
                dev_settings_t st;
                settings_get(&st);
                apply_sm_config();
                bool visual_override = (st.reduced_motion || st.lights_off) && st.condition != COND_B1;
                if (st.reduced_motion != prev.reduced_motion || st.lights_off != prev.lights_off ||
                    st.condition != prev.condition) {
                    if (visual_override)
                        telemetry_log(TEL_SETTINGS_APPLIED, 0, (int32_t)st.version, 1,
                                      st.lights_off ? "access_override:lights_off" : "access_override:reduced_motion");
                }
                prev = st;
                break;
            }
            case MSG_STATUS_PROBE:
                if (s_sm.state == ST_IDLE || m.probe_status < 0) {
                    leds_probe(m.probe_status, m.probe_ms);
                    telemetry_log(TEL_STATUS_PROBE, 0, (int32_t)m.probe_ms, 0,
                                  m.probe_status >= 0 ? led_status_name((led_status_t)m.probe_status) : "end");
                } else {
                    telemetry_log(TEL_STATUS_PROBE, 0, 0, 1, "rejected_not_idle");
                }
                break;
            case MSG_LED_DIAG:
                leds_diag((led_diag_t)m.diag_pattern, 8000);
                break;
            }
        }
        uint32_t now = now_ms();
        if (now - last_tick >= 20) {
            last_tick = now;
            sm_event_t t = {.type = SM_EV_TICK, .now_ms = now};
            handle_sm_event(&t);
        }
    }
}

static void build_boot_diag(const board_status_t *bs)
{
    esp_chip_info_t chip;
    esp_chip_info(&chip);
    audio_stats_t as;
    audio_get_stats(&as);
    char i2c[64] = "";
    for (int i = 0; i < bs->i2c_device_count; i++) {
        char h[6];
        snprintf(h, sizeof(h), "%s0x%02x", i ? "," : "", bs->i2c_devices[i]);
        strlcat(i2c, h, sizeof(i2c));
    }
    char json[900];
    snprintf(json, sizeof(json),
             "{\"boot_id\":\"%s\",\"firmware\":\"%s\",\"idf\":\"%s\",\"chip_model\":\"ESP32-S3\","
             "\"chip_revision\":%d,\"cores\":%d,\"psram_bytes\":%u,\"free_internal\":%u,\"free_psram\":%u,"
             "\"reset_reason\":%d,\"i2c_devices\":\"%s\",\"es8311\":%s,\"es7210\":%s,\"tca9555\":%s,"
             "\"speaker_ok\":%s,\"mic_ok\":%s,\"mic_selftest_left_dbfs\":%.1f,\"mic_selftest_right_dbfs\":%.1f,"
             "\"mic_selftest_zero_frames\":%d,\"mic_selftest_frames\":%d,\"led_count\":%d,\"led_gpio\":%d,"
             "\"button_gpio\":%d,\"sample_rate\":%d}",
             s_boot_id, FIRMWARE_VERSION, esp_get_idf_version(), chip.revision, chip.cores,
             (unsigned)esp_psram_get_size(), (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM), (int)esp_reset_reason(), i2c,
             bs->es8311_found ? "true" : "false", bs->es7210_found ? "true" : "false",
             bs->tca9555_found ? "true" : "false", bs->speaker_ok ? "true" : "false",
             bs->mic_ok ? "true" : "false", as.selftest_rms_left, as.selftest_rms_right,
             as.selftest_zero_frames, as.selftest_frames, LED_COUNT, PIN_LED_DATA, PIN_BUTTON, AUDIO_SAMPLE_RATE);
    ESP_LOGI(TAG, "boot diagnostics: %s", json);
    backend_set_boot_diag(json);
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }
    snprintf(s_boot_id, sizeof(s_boot_id), "%08lx", (unsigned long)esp_random());
    settings_load();
    telemetry_init();
    s_q = xQueueCreate(32, sizeof(ctrl_msg_t));
    sm_config_t sc;
    sm_default_config(&sc);
    sc.processing_timeout_ms = PROCESSING_TIMEOUT_MS;
    sc.continuation_ms = CONTINUATION_WINDOW_MS;
    sc.session_expiry_ms = SESSION_EXPIRY_MS;
    sc.no_speech_timeout_ms = NO_SPEECH_TIMEOUT_MS;
    sm_init(&s_sm, &sc, now_ms());
    apply_sm_config();

    ESP_LOGI(TAG, "[un]scripted 2.0 firmware %s, boot %s, PSRAM %u bytes", FIRMWARE_VERSION, s_boot_id,
             (unsigned)esp_psram_get_size());
    leds_start();                       // BOOTING glow while the rest comes up

    board_status_t bs;
    board_init(&bs);
    audio_init(bs.speaker_ok, bs.mic_ok);
    audio_mic_selftest();
    audio_start();
    telemetry_log(TEL_DEVICE_BOOT, 0, (int32_t)esp_reset_reason(), 0, FIRMWARE_VERSION);
    build_boot_diag(&bs);

    xTaskCreatePinnedToCore(controller_task, "ctrl", 6144, NULL, 8, NULL, 1);
    button_start();
    wifi_start();
    backend_start();
    ctrl_post_event(SM_EV_BOOT_DONE, 0, 0);
}
