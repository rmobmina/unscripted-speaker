// [un]scripted 2.0 — board pin configuration
//
// Target board: Waveshare ESP32-S3-AUDIO-Board (identified from the pins used by
// the existing firmware in this repository, then cross-checked against the
// Waveshare wiki page "ESP32-S3-AUDIO-Board").
//
// Every value below is tagged with its EVIDENCE level:
//   [CHIP]   read programmatically from the connected chip (esptool)
//   [WIKI]   listed on the official Waveshare wiki pin table
//   [PRIOR]  used by earlier firmware in this repo (main/hello_world_main.c,
//            spotify_speaker/main/main.c); assumed working, not re-verified here
//   [VERIFY] NOT documented and NOT verified. Must be confirmed on the physical
//            board before a study session (see docs/HARDWARE_AUDIT.md)
//
// Do not change a pin without recording the evidence for the new value.
#pragma once

#include "driver/gpio.h"

// ---- MCU -----------------------------------------------------------------
// [CHIP] ESP32-S3 (QFN56) rev v0.2, embedded PSRAM 8 MB, 16 MB flash,
//        USB-Serial/JTAG console.

// ---- Shared I2C bus (ES8311 codec, ES7210 ADC, TCA9555 expander, RTC, touch)
#define PIN_I2C_SCL         GPIO_NUM_10   // [WIKI][PRIOR]
#define PIN_I2C_SDA         GPIO_NUM_11   // [WIKI][PRIOR]

// ---- I2S (shared clock lines for speaker codec and microphone ADC) -------
#define PIN_I2S_MCLK        GPIO_NUM_12   // [WIKI][PRIOR]
#define PIN_I2S_BCLK        GPIO_NUM_13   // [WIKI][PRIOR]
#define PIN_I2S_LRCK        GPIO_NUM_14   // [WIKI][PRIOR]
#define PIN_I2S_DOUT        GPIO_NUM_16   // [WIKI][PRIOR] ESP32 -> ES8311 (speaker)
#define PIN_I2S_DIN         GPIO_NUM_15   // [WIKI] ES7210 -> ESP32 (microphones)
                                          // Wiki labels it I2S_ASDOUT; confirmed at
                                          // boot by the mic self-test (non-zero data).

// ---- TCA9555 IO expander --------------------------------------------------
// [PRIOR] address pins strapped to 000; speaker amplifier enable on P10
// (IO_EXPANDER_PIN_NUM_8). The wiki does not list the amp-enable pin.
#define EXPANDER_AMP_EN_PIN 8             // [PRIOR] (IO_EXPANDER_PIN_NUM_8)

// ---- Codecs ----------------------------------------------------------------
// I2C addresses come from esp_codec_dev defaults (ES8311 0x30, ES7210 0x80 as
// 8-bit addresses = 0x18 / 0x40 7-bit). Confirmed by the boot-time I2C scan.
#define ES7210_MIC_MASK     0x03          // MIC1 | MIC2 ("dual mic array" [WIKI])

// ---- Main push button ------------------------------------------------------
// [VERIFY] Default is the on-board BOOT key. GPIO0 is the ESP32-S3 boot
// strapping pin, which carries the BOOT key on Espressif-style boards, but the
// Waveshare wiki gives no GPIO for any key. Confirm by pressing BOOT and
// watching the "button" log lines, or wire the large arcade button to a GPIO
// that the schematic shows as free and set it here.
// NOTE: holding a button on GPIO0 while the board resets enters download mode.
#define PIN_BUTTON          GPIO_NUM_0    // [VERIFY]
#define BUTTON_ACTIVE_LOW   1             // pressed = pulled to GND
#define BUTTON_INTERNAL_PULLUP 1

// ---- Addressable LED ring --------------------------------------------------
// The protocol specifies a 16 x WS2812B ring. The board itself has a 7-LED
// "surround" ring [WIKI] on GPIO38 [PRIOR]. The firmware supports either; set
// LED_COUNT to the number of pixels physically present.
#define PIN_LED_DATA        GPIO_NUM_38   // [PRIOR] on-board 7-LED ring
#define LED_COUNT           7             // [WIKI] on-board ring. Use 16 for the
                                          // external protocol ring once wired,
                                          // and only after verifying its data
                                          // GPIO, 5 V supply and level shifting.
#define LED_IS_RGBW         0
// Logical pixel 0 is the 12 o'clock position, increasing clockwise.
// LED_INDEX_OFFSET = physical index of the pixel at 12 o'clock.
// LED_INDEX_REVERSE = 1 if physical indices run counter-clockwise.
// Both can also be overridden at runtime from the researcher dashboard
// (Device panel -> LED calibration), so the ring can be mounted in any
// orientation without reflashing. [VERIFY] orientation on your enclosure.
#define LED_INDEX_OFFSET    0
#define LED_INDEX_REVERSE   0
// Hard ceiling for the global brightness cap (percent of full scale). The
// dashboard brightness setting can only go up to this value. Keeps worst-case
// current inside what a USB port can supply (16 px * 60 mA = 960 mA at 100%).
#define LED_HARD_MAX_PCT    60
