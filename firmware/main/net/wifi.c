#include "wifi.h"

#include <string.h>

#include "app_config.h"
#include "app_shared.h"
#include "backend.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "secrets.h"
#include "telemetry.h"

static const char *TAG = "wifi";
static volatile bool s_up;
static uint32_t s_backoff_ms = WIFI_BACKOFF_MIN_MS;
static esp_timer_handle_t s_retry_timer;

bool wifi_is_up(void) { return s_up; }

int wifi_rssi(void)
{
    wifi_ap_record_t ap;
    return esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : 0;
}

static void retry_cb(void *arg) { esp_wifi_connect(); }

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *d = data;
        if (s_up) telemetry_log(TEL_WIFI_DISCONNECTED, 0, 0, d ? d->reason : 0, NULL);
        s_up = false;
        backend_net_changed();
        // Bounded exponential backoff, scheduled on a timer (never block the event loop).
        ESP_LOGW(TAG, "disconnected (reason %d); retry in %lu ms", d ? d->reason : -1, (unsigned long)s_backoff_ms);
        esp_timer_stop(s_retry_timer);
        esp_timer_start_once(s_retry_timer, (uint64_t)s_backoff_ms * 1000);
        s_backoff_ms = s_backoff_ms * 2 > WIFI_BACKOFF_MAX_MS ? WIFI_BACKOFF_MAX_MS : s_backoff_ms * 2;
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "connected, device IP " IPSTR, IP2STR(&e->ip_info.ip));
        s_backoff_ms = WIFI_BACKOFF_MIN_MS;
        s_up = true;
        telemetry_log(TEL_WIFI_CONNECTED, 0, wifi_rssi(), 0, NULL);
        backend_net_changed();
    }
}

void wifi_start(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
    const esp_timer_create_args_t targs = {.callback = retry_cb, .name = "wifi_retry"};
    ESP_ERROR_CHECK(esp_timer_create(&targs, &s_retry_timer));

    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, UNS_WIFI_SSID, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, UNS_WIFI_PASSWORD, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = WIFI_AUTH_OPEN;
    wc.sta.pmf_cfg.capable = true;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));   // credentials are not copied into NVS
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_ps(WIFI_PS_NONE);     // lowest latency for audio
    ESP_LOGI(TAG, "connecting to the configured 2.4 GHz network");   // SSID not logged
}
