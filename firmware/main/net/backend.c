#include "backend.h"

#include <stdio.h>
#include <string.h>

#include "app_config.h"
#include "app_shared.h"
#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hw/audio.h"
#include "hw/leds.h"
#include "secrets.h"
#include "telemetry.h"
#include "wifi.h"

static const char *TAG = "backend";

static SemaphoreHandle_t s_worker_slots;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static bool s_backend_ok;
static bool s_online_reported;     // last NET_UP/NET_DOWN sent to the controller
static bool s_online_initialised;
static uint32_t s_cancel_q[4];
static int s_cancel_n;
static uint32_t s_followup_turn[4];
static int s_followup_i;
static char *s_boot_diag;

typedef struct {
    uint32_t turn;
    uint8_t *pcm;
    size_t bytes;
    uint32_t dur_ms;
    int quality;
    bool followup;
} turn_job_t;

static bool is_https(void) { return strncmp(UNS_BACKEND_URL, "https://", 8) == 0; }

static esp_http_client_handle_t http_new(const char *path, esp_http_client_method_t method, int timeout_ms)
{
    char url[256];
    snprintf(url, sizeof(url), "%s%s", UNS_BACKEND_URL, path);
    esp_http_client_config_t cfg = {
        .url = url,
        .method = method,
        .timeout_ms = timeout_ms,
        .buffer_size = 4096,
        .buffer_size_tx = 1536,
        .keep_alive_enable = false,
        .crt_bundle_attach = is_https() ? esp_crt_bundle_attach : NULL,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return NULL;
    esp_http_client_set_header(c, "Authorization", "Bearer " UNS_DEVICE_TOKEN);
    esp_http_client_set_header(c, "X-Device-Id", UNS_DEVICE_ID);
    esp_http_client_set_header(c, "X-Firmware-Version", FIRMWARE_VERSION);
    esp_http_client_set_header(c, "X-Boot-Id", device_boot_id());
    esp_http_client_set_header(c, "X-Client-Kind", "device");
    return c;
}

// Reads the whole (small) response body into buf. Returns length or -1.
static int read_body(esp_http_client_handle_t c, char *buf, int cap)
{
    int len = 0;
    for (;;) {
        int r = esp_http_client_read(c, buf + len, cap - 1 - len);
        if (r < 0) return -1;
        if (r == 0) break;
        len += r;
        if (len >= cap - 1) break;
    }
    buf[len] = 0;
    return len;
}

// ---------------------------------------------------------------- online ---
static void report_online(void)
{
    bool online;
    bool changed = false;
    taskENTER_CRITICAL(&s_lock);
    online = wifi_is_up() && s_backend_ok;
    if (!s_online_initialised || online != s_online_reported) {
        s_online_initialised = true;
        s_online_reported = online;
        changed = true;
    }
    taskEXIT_CRITICAL(&s_lock);
    if (changed) ctrl_post_event(online ? SM_EV_NET_UP : SM_EV_NET_DOWN, 0, 0);
}

void backend_net_changed(void) { report_online(); }
bool backend_online(void) { return s_online_reported; }

void backend_set_boot_diag(const char *json)
{
    s_boot_diag = strdup(json);
}

void backend_mark_followup(uint32_t turn, bool followup)
{
    taskENTER_CRITICAL(&s_lock);
    s_followup_turn[s_followup_i++ % 4] = followup ? turn : 0;
    taskEXIT_CRITICAL(&s_lock);
}

static bool is_followup(uint32_t turn)
{
    bool f = false;
    taskENTER_CRITICAL(&s_lock);
    for (int i = 0; i < 4; i++) f |= (s_followup_turn[i] == turn);
    taskEXIT_CRITICAL(&s_lock);
    return f;
}

void backend_cancel_turn(uint32_t turn)
{
    if (!turn) return;
    taskENTER_CRITICAL(&s_lock);
    if (s_cancel_n < 4) s_cancel_q[s_cancel_n++] = turn;
    taskEXIT_CRITICAL(&s_lock);
}

// ------------------------------------------------------------ turn worker ---
static int topic_from_string(const char *t)
{
    static const char *names[] = {"general", "reassurance", "family", "routine", "relaxation"};
    for (int i = 0; i < 5; i++) if (t && strcmp(t, names[i]) == 0) return i;
    return 0;
}

static void fail(uint32_t turn, sm_error_t e, const char *why)
{
    ESP_LOGW(TAG, "turn %lu failed: %s", (unsigned long)turn, why);
    telemetry_log(TEL_ERROR, turn, 0, e, why);
    ctrl_post_event(SM_EV_TURN_FAILED, turn, e);
}

static void stream_reply_audio(uint32_t turn, const char *turn_id)
{
    char path[128];
    snprintf(path, sizeof(path), "/api/v1/device/turns/%s/audio", turn_id);
    esp_http_client_handle_t c = http_new(path, HTTP_METHOD_GET, 10000);
    bool failed = true;
    if (c && esp_http_client_open(c, 0) == ESP_OK) {
        esp_http_client_fetch_headers(c);
        int status = esp_http_client_get_status_code(c);
        uint8_t *chunk = status == 200 ? malloc(2048) : NULL;
        if (chunk) {
            int carry = 0;   // keep sample alignment (2 bytes)
            for (;;) {
                if (active_turn() != turn) {
                    failed = false;   // cancelled, not a failure
                    break;
                }
                int r = esp_http_client_read(c, (char *)chunk + carry, 2048 - carry);
                if (r < 0) break;
                if (r == 0) {
                    failed = !esp_http_client_is_complete_data_received(c);
                    break;
                }
                int total = carry + r;
                int even = total & ~1;
                if (even > 0 && playback_push(turn, chunk, (size_t)even, 5000) < 0) {
                    failed = false;   // playback stopped by an interruption
                    break;
                }
                carry = total - even;
                if (carry) chunk[0] = chunk[even];
            }
            free(chunk);
        } else {
            ESP_LOGW(TAG, "audio GET status %d", status);
        }
    }
    playback_finish(turn, failed);
    if (c) {
        esp_http_client_close(c);
        esp_http_client_cleanup(c);
    }
}

static void turn_worker(void *arg)
{
    turn_job_t *job = arg;
    uint32_t turn = job->turn;
    char turn_id[48], sid[48], hdr[16];
    turn_id_string(turn, turn_id, sizeof(turn_id));
    session_get(sid, sizeof(sid));
    dev_settings_t st;
    settings_get(&st);

    esp_http_client_handle_t c = http_new("/api/v1/device/turns", HTTP_METHOD_POST, TURN_HTTP_TIMEOUT_MS);
    if (!c) {
        fail(turn, SM_ERR_BACKEND, "http_init");
        goto done;
    }
    esp_http_client_set_header(c, "Content-Type", "application/octet-stream");
    esp_http_client_set_header(c, "X-Audio-Format", "pcm_s16le;rate=16000;channels=1");
    esp_http_client_set_header(c, "X-Turn-Id", turn_id);
    esp_http_client_set_header(c, "X-Session-Id", sid);
    snprintf(hdr, sizeof(hdr), "B%d", (int)st.condition);
    esp_http_client_set_header(c, "X-Condition", hdr);
    esp_http_client_set_header(c, "X-Followup", job->followup ? "1" : "0");
    snprintf(hdr, sizeof(hdr), "%lu", (unsigned long)job->dur_ms);
    esp_http_client_set_header(c, "X-Capture-Ms", hdr);
    snprintf(hdr, sizeof(hdr), "%d", job->quality);
    esp_http_client_set_header(c, "X-Capture-Quality", hdr);

    uint32_t t0 = now_ms();
    telemetry_log(TEL_AUDIO_UPLOAD_STARTED, turn, (int32_t)job->bytes, 0, NULL);
    if (esp_http_client_open(c, (int)job->bytes) != ESP_OK) {
        fail(turn, SM_ERR_BACKEND, "connect");
        goto cleanup;
    }
    size_t sent = 0;
    while (sent < job->bytes) {
        if (active_turn() != turn) {   // cancelled: abort without completing the body
            telemetry_log(TEL_STALE_RESPONSE_REJECTED, turn, (int32_t)sent, 0, "upload_aborted");
            goto cleanup;
        }
        size_t n = job->bytes - sent > 4096 ? 4096 : job->bytes - sent;
        int w = esp_http_client_write(c, (const char *)job->pcm + sent, (int)n);
        if (w <= 0) {
            fail(turn, SM_ERR_BACKEND, "upload");
            goto cleanup;
        }
        sent += (size_t)w;
    }
    telemetry_log(TEL_AUDIO_UPLOAD_COMPLETED, turn, (int32_t)(now_ms() - t0), 0, NULL);
    heap_caps_free(job->pcm);   // raw audio leaves device memory as soon as it is sent
    job->pcm = NULL;

    esp_http_client_fetch_headers(c);
    int status = esp_http_client_get_status_code(c);
    char body[2048];
    int blen = read_body(c, body, sizeof(body));

    if (active_turn() != turn) {
        telemetry_log(TEL_STALE_RESPONSE_REJECTED, turn, status, 0, "reply_after_cancel");
        goto cleanup;
    }
    if (blen < 0) {
        fail(turn, SM_ERR_BACKEND, "read");
        goto cleanup;
    }
    cJSON *root = cJSON_Parse(body);
    if (status == 503) {
        fail(turn, SM_ERR_AI_UNAVAILABLE, "ai_unavailable");
        cJSON_Delete(root);
        goto cleanup;
    }
    if (status != 200 || !root) {
        char why[24];
        snprintf(why, sizeof(why), "http_%d", status);
        fail(turn, status == 504 ? SM_ERR_TIMEOUT : SM_ERR_BACKEND, why);
        cJSON_Delete(root);
        goto cleanup;
    }
    const cJSON *rid = cJSON_GetObjectItem(root, "turn_id");
    if (!cJSON_IsString(rid) || strcmp(rid->valuestring, turn_id) != 0) {
        fail(turn, SM_ERR_BACKEND, "turn_id_mismatch");   // never play another turn's audio
        cJSON_Delete(root);
        goto cleanup;
    }
    const cJSON *sess = cJSON_GetObjectItem(root, "session_id");
    if (cJSON_IsString(sess)) session_set(sess->valuestring);
    const cJSON *outcome = cJSON_GetObjectItem(root, "outcome");
    const char *oc = cJSON_IsString(outcome) ? outcome->valuestring : "";
    telemetry_log(TEL_REPLY_RECEIVED, turn, (int32_t)(now_ms() - t0), status, oc);

    if (strcmp(oc, "not_understood") == 0) {
        ctrl_post_event(SM_EV_REPLY_NOT_UNDERSTOOD, turn, 0);
        cJSON_Delete(root);
        goto cleanup;
    }
    if (strcmp(oc, "reply") != 0) {
        fail(turn, SM_ERR_BACKEND, "bad_outcome");
        cJSON_Delete(root);
        goto cleanup;
    }
    const cJSON *reply = cJSON_GetObjectItem(root, "reply");
    const cJSON *topic = reply ? cJSON_GetObjectItem(reply, "topic") : NULL;
    const cJSON *conf = reply ? cJSON_GetObjectItem(reply, "topic_confidence") : NULL;
    ctrl_msg_t m = {.type = MSG_REPLY_META,
                    .ev = {.turn = turn},
                    .topic = topic_from_string(cJSON_IsString(topic) ? topic->valuestring : NULL),
                    .topic_confidence = cJSON_IsNumber(conf) ? (float)conf->valuedouble : 0.f};
    cJSON_Delete(root);
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    c = NULL;

    playback_begin(turn);
    ctrl_post(&m);
    ctrl_post_event(SM_EV_REPLY_READY, turn, 0);
    stream_reply_audio(turn, turn_id);

cleanup:
    if (c) {
        esp_http_client_close(c);
        esp_http_client_cleanup(c);
    }
done:
    if (job->pcm) heap_caps_free(job->pcm);
    free(job);
    xSemaphoreGive(s_worker_slots);
    vTaskDelete(NULL);
}

bool backend_submit_turn(uint32_t turn, const int16_t *pcm, size_t bytes, uint32_t dur_ms, int quality)
{
    if (xSemaphoreTake(s_worker_slots, 0) != pdTRUE) {
        ESP_LOGW(TAG, "no free turn worker");
        return false;
    }
    turn_job_t *job = calloc(1, sizeof(*job));
    uint8_t *copy = heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!job || !copy) {
        free(job);
        if (copy) heap_caps_free(copy);
        xSemaphoreGive(s_worker_slots);
        return false;
    }
    memcpy(copy, pcm, bytes);
    *job = (turn_job_t){turn, copy, bytes, dur_ms, quality, is_followup(turn)};
    if (xTaskCreatePinnedToCore(turn_worker, "turn", 8192, job, 5, NULL, 0) != pdPASS) {
        heap_caps_free(copy);
        free(job);
        xSemaphoreGive(s_worker_slots);
        return false;
    }
    return true;
}

// ------------------------------------------------------- config / telemetry ---
static uint32_t s_probe_seq, s_diag_seq;
static bool s_first_config = true;

static int status_from_name(const char *s)
{
    static const char *n[] = {"BOOTING", "IDLE", "LISTENING", "PROCESSING", "SPEAKING", "ERROR", "OFFLINE"};
    for (int i = 0; i < 7; i++) if (s && strcmp(s, n[i]) == 0) return i;
    return -1;
}

static void apply_config_json(const cJSON *j)
{
    dev_settings_t cur, st;
    settings_get(&cur);
    st = cur;
#define NUM(key, field, lo, hi)                                                    \
    do {                                                                            \
        const cJSON *x = cJSON_GetObjectItem(j, key);                               \
        if (cJSON_IsNumber(x) && x->valuedouble >= (lo) && x->valuedouble <= (hi))  \
            st.field = x->valuedouble;                                              \
    } while (0)
#define BOOL(key, field)                                                            \
    do {                                                                            \
        const cJSON *x = cJSON_GetObjectItem(j, key);                               \
        if (cJSON_IsBool(x)) st.field = cJSON_IsTrue(x);                            \
    } while (0)
    NUM("version", version, 0, 4e9);
    const cJSON *cond = cJSON_GetObjectItem(j, "condition");
    if (cJSON_IsString(cond)) {
        if (!strcmp(cond->valuestring, "B1")) st.condition = COND_B1;
        else if (!strcmp(cond->valuestring, "B2")) st.condition = COND_B2;
        else if (!strcmp(cond->valuestring, "B3")) st.condition = COND_B3;
    }
    BOOL("reduced_motion", reduced_motion);
    BOOL("lights_off", lights_off);
    NUM("max_brightness_pct", max_brightness_pct, 0, 100);
    NUM("volume", volume, 0, 100);
    NUM("led_offset", led_offset, 0, 63);
    BOOL("led_reverse", led_reverse);
    BOOL("led_grb", led_grb);
    NUM("afterglow_ms", afterglow_ms, 0, 60000);
    NUM("silence_endpoint_ms", silence_endpoint_ms, 500, 5000);
    BOOL("endpoint_while_held", endpoint_while_held);
    NUM("vad_abs_dbfs", vad_abs_dbfs, -90, -10);
    NUM("vad_snr_db", vad_snr_db, 0, 40);
    NUM("session_reset_seq", session_reset_seq, 0, 4e9);
#undef NUM
#undef BOOL
    if (st.session_reset_seq != cur.session_reset_seq && !s_first_config) {
        session_clear();
        telemetry_log(TEL_SESSION_RESET, 0, (int32_t)st.session_reset_seq, 0, "dashboard");
    }
    settings_apply(&st);
    if (st.condition != cur.condition || s_first_config) {
        char d[8];
        snprintf(d, sizeof(d), "B%d", (int)st.condition);
        telemetry_log(TEL_CONDITION_APPLIED, 0, (int32_t)st.version, 0, d);
    }
    telemetry_log(TEL_SETTINGS_APPLIED, 0, (int32_t)st.version, 0, NULL);
    ctrl_msg_t m = {.type = MSG_SETTINGS};
    ctrl_post(&m);

    const cJSON *probe = cJSON_GetObjectItem(j, "probe");
    const cJSON *pseq = probe ? cJSON_GetObjectItem(probe, "seq") : NULL;
    if (cJSON_IsNumber(pseq) && (uint32_t)pseq->valuedouble != s_probe_seq) {
        bool first = s_first_config;
        s_probe_seq = (uint32_t)pseq->valuedouble;
        const cJSON *ps = cJSON_GetObjectItem(probe, "status");
        const cJSON *pd = cJSON_GetObjectItem(probe, "duration_ms");
        if (!first) {   // never replay an old probe after reboot
            ctrl_msg_t pm = {.type = MSG_STATUS_PROBE,
                             .probe_status = status_from_name(cJSON_IsString(ps) ? ps->valuestring : NULL),
                             .probe_ms = cJSON_IsNumber(pd) ? (uint32_t)pd->valuedouble : 8000};
            ctrl_post(&pm);
        }
    }
    const cJSON *diag = cJSON_GetObjectItem(j, "diag");
    const cJSON *dseq = diag ? cJSON_GetObjectItem(diag, "seq") : NULL;
    if (cJSON_IsNumber(dseq) && (uint32_t)dseq->valuedouble != s_diag_seq) {
        bool first = s_first_config;
        s_diag_seq = (uint32_t)dseq->valuedouble;
        const cJSON *pat = cJSON_GetObjectItem(diag, "pattern");
        int p = LED_DIAG_NONE;
        if (cJSON_IsString(pat)) {
            if (!strcmp(pat->valuestring, "logical_zero")) p = LED_DIAG_LOGICAL_ZERO;
            else if (!strcmp(pat->valuestring, "rgb")) p = LED_DIAG_RGB;
            else if (!strcmp(pat->valuestring, "all_white_low")) p = LED_DIAG_ALL_WHITE_LOW;
        }
        if (!first && p != LED_DIAG_NONE) {
            ctrl_msg_t dm = {.type = MSG_LED_DIAG, .diag_pattern = p};
            ctrl_post(&dm);
        }
    }
    const cJSON *srv = cJSON_GetObjectItem(j, "server_time_ms");
    if (cJSON_IsNumber(srv)) telemetry_set_wall_offset((int64_t)srv->valuedouble - (int64_t)now_ms());
    s_first_config = false;
}

static bool poll_config(void)
{
    esp_http_client_handle_t c = http_new("/api/v1/device/config", HTTP_METHOD_GET, CONFIG_POLL_TIMEOUT_MS);
    if (!c) return false;
    dev_settings_t st;
    settings_get(&st);
    char v[16];
    snprintf(v, sizeof(v), "\"%lu\"", (unsigned long)st.version);
    if (!s_first_config) esp_http_client_set_header(c, "If-None-Match", v);
    snprintf(v, sizeof(v), "%lu", (unsigned long)st.version);
    esp_http_client_set_header(c, "X-Applied-Version", v);
    esp_http_client_set_header(c, "X-Device-State", sm_state_name(device_state()));
    snprintf(v, sizeof(v), "%d", wifi_rssi());
    esp_http_client_set_header(c, "X-Wifi-Rssi", v);
    bool ok = false;
    if (esp_http_client_open(c, 0) == ESP_OK) {
        esp_http_client_fetch_headers(c);
        int status = esp_http_client_get_status_code(c);
        if (status == 304) {
            ok = true;
        } else if (status == 200) {
            char body[1536];
            if (read_body(c, body, sizeof(body)) > 0) {
                cJSON *j = cJSON_Parse(body);
                if (j) {
                    apply_config_json(j);
                    cJSON_Delete(j);
                    ok = true;
                }
            }
        } else if (status == 401) {
            ESP_LOGE(TAG, "backend rejected the device token (401) - re-provision the device");
        }
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    return ok;
}

static void send_cancels(void)
{
    uint32_t q[4];
    int n;
    taskENTER_CRITICAL(&s_lock);
    n = s_cancel_n;
    memcpy(q, s_cancel_q, sizeof(q));
    s_cancel_n = 0;
    taskEXIT_CRITICAL(&s_lock);
    for (int i = 0; i < n; i++) {
        char id[48], path[96];
        turn_id_string(q[i], id, sizeof(id));
        snprintf(path, sizeof(path), "/api/v1/device/turns/%s", id);
        esp_http_client_handle_t c = http_new(path, HTTP_METHOD_DELETE, 2000);
        if (c) {
            esp_http_client_perform(c);
            esp_http_client_cleanup(c);
        }
    }
}

static void flush_telemetry(void)
{
    static tel_event_t ev[TELEMETRY_BATCH];
    int n = telemetry_peek(ev, TELEMETRY_BATCH);
    if (n == 0) return;
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "boot_id", device_boot_id());
    cJSON_AddNumberToObject(root, "dropped", telemetry_dropped());
    cJSON *arr = cJSON_AddArrayToObject(root, "events");
    for (int i = 0; i < n; i++) {
        cJSON *o = cJSON_CreateObject();
        char tid[48] = "";
        if (ev[i].turn) turn_id_string(ev[i].turn, tid, sizeof(tid));
        char cond[12];
        snprintf(cond, sizeof(cond), "B%d", ev[i].condition);
        cJSON_AddStringToObject(o, "name", telemetry_name((tel_name_t)ev[i].name));
        cJSON_AddNumberToObject(o, "t_mono_ms", ev[i].t_ms);
        if (ev[i].wall_ms) cJSON_AddNumberToObject(o, "wall_ms", (double)ev[i].wall_ms);
        cJSON_AddStringToObject(o, "turn_id", tid);
        cJSON_AddStringToObject(o, "state", sm_state_name((dev_state_t)ev[i].state));
        cJSON_AddStringToObject(o, "condition", cond);
        cJSON_AddNumberToObject(o, "value", ev[i].value);
        cJSON_AddNumberToObject(o, "code", ev[i].code);
        cJSON_AddStringToObject(o, "detail", ev[i].detail);
        cJSON_AddItemToArray(arr, o);
    }
    char *json = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!json) return;
    esp_http_client_handle_t c = http_new("/api/v1/device/events", HTTP_METHOD_POST, 3000);
    if (c) {
        esp_http_client_set_header(c, "Content-Type", "application/json");
        esp_http_client_set_post_field(c, json, (int)strlen(json));
        if (esp_http_client_perform(c) == ESP_OK && esp_http_client_get_status_code(c) / 100 == 2) {
            telemetry_drop(n);
        }
        esp_http_client_cleanup(c);
    }
    free(json);
}

static void send_boot_diag(void)
{
    if (!s_boot_diag) return;
    esp_http_client_handle_t c = http_new("/api/v1/device/diagnostics", HTTP_METHOD_POST, 3000);
    if (c) {
        esp_http_client_set_header(c, "Content-Type", "application/json");
        esp_http_client_set_post_field(c, s_boot_diag, (int)strlen(s_boot_diag));
        if (esp_http_client_perform(c) == ESP_OK && esp_http_client_get_status_code(c) / 100 == 2) {
            free(s_boot_diag);
            s_boot_diag = NULL;
        }
        esp_http_client_cleanup(c);
    }
}

static void config_task(void *arg)
{
    int fails = 0;
    TickType_t last = xTaskGetTickCount();
    uint32_t last_led_stats = 0;
    for (;;) {
        if (wifi_is_up()) {
            bool ok = poll_config();
            if (ok) {
                if (!s_backend_ok) telemetry_log(TEL_BACKEND_CONNECTED, 0, 0, 0, NULL);
                fails = 0;
                s_backend_ok = true;
            } else if (++fails >= BACKEND_FAILS_OFFLINE) {
                if (s_backend_ok) telemetry_log(TEL_BACKEND_UNREACHABLE, 0, fails, 0, NULL);
                s_backend_ok = false;
            }
            report_online();
            if (s_backend_ok) {
                send_cancels();
                send_boot_diag();
                if (now_ms() - last_led_stats > 60000) {
                    led_stats_t ls;
                    leds_get_stats(&ls);
                    char d[28];
                    snprintf(d, sizeof(d), "frames=%lu", (unsigned long)ls.frames);
                    telemetry_log(TEL_LED_STATS, 0, (int32_t)ls.late_frames, (int)ls.max_render_us, d);
                    last_led_stats = now_ms();
                }
                flush_telemetry();
            }
        } else {
            fails = 0;
            s_backend_ok = false;
            report_online();
        }
        vTaskDelayUntil(&last, pdMS_TO_TICKS(CONFIG_POLL_MS));
        if (xTaskGetTickCount() - last > pdMS_TO_TICKS(5 * CONFIG_POLL_MS)) last = xTaskGetTickCount();
    }
}

void backend_start(void)
{
    s_worker_slots = xSemaphoreCreateCounting(MAX_TURN_WORKERS, MAX_TURN_WORKERS);
    xTaskCreatePinnedToCore(config_task, "config", 8192, NULL, 4, NULL, 0);
}
