#include "button.h"

#include "app_config.h"
#include "app_shared.h"
#include "board.h"
#include "core/button_logic.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pins.h"

static const char *TAG = "button";
static btn_logic_t s_btn;

static void sample_cb(void *arg)
{
    int level = gpio_get_level(PIN_BUTTON);
    bool pressed = BUTTON_ACTIVE_LOW ? (level == 0) : (level != 0);
    btn_event_t e = btn_update(&s_btn, pressed, now_ms());
    switch (e) {
    case BTN_EV_PRESS: ctrl_post_event(SM_EV_BTN_PRESS, 0, 0); break;
    case BTN_EV_RELEASE: ctrl_post_event(SM_EV_BTN_RELEASE, 0, 0); break;
    case BTN_EV_LONG: ctrl_post_event(SM_EV_BTN_LONG, 0, 0); break;
    default: break;
    }
}

static void expander_discovery_task(void *arg)
{
    // Diagnostic only: report which expander input bit changes when a key is
    // pressed. Runs for 120 s after boot and then exits.
    uint32_t last = 0;
    bool have = false;
    for (int i = 0; i < 120 * 10; i++) {
        uint32_t lv;
        if (board_expander_inputs(&lv) == ESP_OK) {
            if (have && lv != last) {
                ESP_LOGI(TAG, "[discovery] TCA9555 inputs changed 0x%04lx -> 0x%04lx (bits 0x%04lx)",
                         (unsigned long)last, (unsigned long)lv, (unsigned long)(last ^ lv));
            }
            last = lv;
            have = true;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    ESP_LOGI(TAG, "[discovery] expander key discovery finished");
    vTaskDelete(NULL);
}

void button_start(void)
{
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << PIN_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = BUTTON_INTERNAL_PULLUP ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io));
    btn_init(&s_btn, BUTTON_DEBOUNCE_MS, BUTTON_LONG_PRESS_MS);

    const esp_timer_create_args_t args = {.callback = sample_cb, .name = "button"};
    esp_timer_handle_t t;
    ESP_ERROR_CHECK(esp_timer_create(&args, &t));
    ESP_ERROR_CHECK(esp_timer_start_periodic(t, BUTTON_POLL_MS * 1000));
    ESP_LOGI(TAG, "button on GPIO%d (active %s) - press it to see 'button' events", PIN_BUTTON,
             BUTTON_ACTIVE_LOW ? "low" : "high");
    xTaskCreatePinnedToCore(expander_discovery_task, "keyscan", 3072, NULL, 2, NULL, 0);
}
