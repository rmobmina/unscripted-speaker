// Main button: sampled every 5 ms by an esp_timer callback, debounced by the
// pure btn_logic module, and forwarded to the controller as state-machine
// events. Also logs TCA9555 input changes during the first two minutes after
// boot so the on-board K1-K3 keys can be identified without guessing.
#pragma once

void button_start(void);
